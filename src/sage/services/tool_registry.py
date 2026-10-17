"""Validated read-only conversational tools for drive Q&A."""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import time
import re
from datetime import datetime, timezone
from .known_folders import describe
from .cleanup import find_cleanup_suggestions
from .evidence import format_bytes

from .local_database import DriveIndex
from .retrieval import ScanEvidence

FACT_FIELDS=['size','logical_size','modified','files','folders','items','direct_files','direct_folders','direct_items']


@dataclass(frozen=True)
class ToolSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


class ToolRegistry:
    """Executes only approved read-only tools with strict argument validation."""

    _SPECS: dict[str, ToolSpec] = {
        "search_names": ToolSpec(("query",), ("scope", "limit")),
        "search_content": ToolSpec(("query",), ("scope", "limit")),
        "inspect_path": ToolSpec(("path",)),
        "list_children": ToolSpec(("path",), ("limit",)),
        "summarize_folder": ToolSpec(("path",)),
        "find_large_files": ToolSpec((), ("scope", "limit")),
        "retrieve_context": ToolSpec(("query",), ("scope", "limit")),
        "impact_analysis": ToolSpec(("path", "action")),
        "search_files": ToolSpec((), ("query","scope","kind","extension","minimum_bytes","modified_before","modified_after","changed_within_days","unchanged_for_days","after_date","before_date","sort","limit")),
        "drive_overview": ToolSpec(()),
        "find_duplicates": ToolSpec((), ("scope","limit")),
        "find_references": ToolSpec(("query",), ("scope","limit")),
        "cleanup_candidates": ToolSpec((), ("scope","limit")),
        "report_fact": ToolSpec(("field",),("path","final")),
    }

    def __init__(self, evidence: ScanEvidence | None, index: DriveIndex | None) -> None:
        self.evidence = evidence
        self.index = index
        self.focus=[]
        self._cleanup_cache=None

    def cleanup_suggestions(self):
        if not self.evidence or not self.evidence.result.sizes_complete:return []
        if self._cleanup_cache is None:self._cleanup_cache=find_cleanup_suggestions(self.evidence.result)
        return self._cleanup_cache

    def validate(self, name: str, arguments: dict) -> None:
        if not isinstance(arguments,dict):
            raise ValueError('Tool arguments must be an object.')
        if name not in self._SPECS:
            raise ValueError(f"Unknown tool: {name}")
        spec = self._SPECS[name]
        missing = [key for key in spec.required if key not in arguments]
        if missing:
            raise ValueError(f"Missing required arguments for {name}: {', '.join(missing)}")
        allowed = set(spec.required) | set(spec.optional)
        extra = [key for key in arguments if key not in allowed]
        if extra:
            raise ValueError(f"Unexpected arguments for {name}: {', '.join(extra)}")
        for key,value in arguments.items():
            if key=='final':
                if type(value) is not bool:raise ValueError('final must be a boolean.')
            elif key in {'changed_within_days','unchanged_for_days'}:
                if type(value) is not int or not 1<=value<=365000:raise ValueError('Invalid day range.')
            elif key in {'limit','minimum_bytes','modified_before','modified_after'}:
                if type(value) is not int or value < 0 or value>2**63-1 or key=='limit' and not 1 <= value <= 20:
                    raise ValueError(f'Invalid numeric argument: {key}')
            elif not isinstance(value,str) or not value.strip() or len(value)>(32767 if key in {'path','scope'} else 1000):
                raise ValueError(f'Invalid text argument: {key}')
        if arguments.get('kind','any') not in {'any','file','folder'} or arguments.get('sort','largest') not in {'largest','recent','oldest','name'}:
            raise ValueError('Invalid kind or sort.')
        if 'field' in arguments and arguments['field'] not in FACT_FIELDS:
            raise ValueError('Invalid fact field.')
        if self.evidence:
            for key in ('path','scope'):
                if key in arguments and 'error' in self.evidence.inspect(arguments[key]):
                    raise ValueError('Path must exist in the current scan.')

    def definitions(self) -> list:
        """Small native tool descriptions; the model chooses meaning, code validates arguments."""
        fields = {'path':{'type':'string'},'scope':{'type':'string'},'query':{'type':'string'},
                  'field':{'type':'string','enum':FACT_FIELDS},
                  'final':{'type':'boolean','description':'true ONLY if this fact fully answers the question; false if advice or explanation is also needed.'},
                  'limit':{'type':'integer','minimum':1,'maximum':20},
                  'kind':{'type':'string','enum':['any','file','folder']},
                  'sort':{'type':'string','enum':['largest','recent','oldest','name']},
                  'extension':{'type':'string','description':'For example .mp4'},
                  'minimum_bytes':{'type':'integer'},'modified_before':{'type':'integer','description':'Unix seconds'},
                  'modified_after':{'type':'integer','description':'Unix seconds'}}
        fields.update({key:{'type':'integer'} for key in ('changed_within_days','unchanged_for_days')})
        fields.update({key:{'type':'string'} for key in ('after_date','before_date')})
        descriptions={
            'report_fact':'Look up size/date/count. Set final=true ONLY for a pure fact question, never cleanup/impact advice. Omit path for one selected item. size=disk space; logical_size=data size; modified=last change; files/folders/items=recursive counts; direct_* counts=direct only.',
            'inspect_path':'Inspect a verified item before advice: nested tree, file types, metadata, purpose clues and ownership evidence. Partial coverage never proves safety.',
            'search_files':'Find files/folders by short name, type, size or dates. Dates: use day ranges or ISO after_date/before_date (YYYY-MM-DD, UTC), not timestamp arithmetic. Never use the whole question as query.',
            'list_children':'List direct children of a folder. Use paths from the results for further inspection.',
            'drive_overview':'Get whole-drive counts and size totals.',
            'search_content':'Search approved local text excerpts. Missing results never prove no reference exists.',
            'find_references':'Find text that mentions a filename or identifier. Matches are clues, not verified runtime dependencies.',
            'find_duplicates':'Find possible duplicates by measured size. File contents are not compared.',
            'cleanup_candidates':'Get existing cleanup rules and their conditions. No files are changed.'}
        return [{'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':{key:fields[key] for key in (*self._SPECS[name].required,*self._SPECS[name].optional)},
                          'required':list(self._SPECS[name].required),'additionalProperties':False}}}
                for name,description in descriptions.items()]

    def guide(self):
        lines=[]
        for spec in self.definitions():
            function=spec['function'];schema=function['parameters']
            fields=[]
            for key,value in schema['properties'].items():
                kind='/'.join(value.get('enum',[])) or value['type']
                fields.append(key+('' if key in schema['required'] else '?')+':'+kind)
            lines.append(function['name']+'('+', '.join(fields)+'): '+function['description'])
        return '\n'.join(lines)

    @staticmethod
    def _limit(value: object, default: int = 8) -> int:
        if value is None:
            return default
        try:
            return max(1, min(int(value), 50))
        except (TypeError, ValueError):
            return default

    def call(self, name: str, arguments: dict) -> dict:
        self.validate(name, arguments)
        arguments = dict(arguments)
        if name=='report_fact':
            if not self.evidence: return {'error':'No scan loaded.'}
            if 'path' not in arguments:
                if len(self.focus)!=1: return {'error':'Supply a verified path, or select one item.'}
                arguments['path']=self.focus[0]
            item=self.evidence.inspect(arguments['path'])
            if 'error' in item: return item
            path=item['path'];field=arguments['field']
            if field in {'size','logical_size'}:
                value=item['allocated_display' if field=='size' else 'logical_display']
                data,space=item['logical_display'],item['allocated_display']
                answer=f'`{path}` uses {space} of disk space.'
                if data!=space and 'pending' not in {data,space}:
                    answer=f'`{path}` contains {data} of data and uses {space} of disk space.'
                if value=='pending': answer=f'The size of `{path}` is still being measured.'
            elif field=='modified':
                stamp=self.evidence.result.records[item['frn']].modified
                value=datetime.fromtimestamp(stamp,timezone.utc).date().isoformat() if stamp else None
                answer=f'`{path}` was last modified on {value} (UTC).' if value else f'The scan has no modification date for `{path}`.'
            elif field.startswith('direct_'):
                children=self.evidence.result.children.get(item['frn'],())
                value=sum(1 for frn in children if field=='direct_items' or
                          self.evidence.result.records[frn].is_directory==(field=='direct_folders'))
                answer=f'`{path}` contains {value:,} {field.removeprefix("direct_")} directly inside it.'
            else:
                counts=self.evidence.result.stats.get(item['frn'],(0,0,0))
                value=counts[{'items':0,'files':1,'folders':2}[field]]
                answer=f'There are {value:,} {field} in `{path}` and its subfolders.'
                if item['kind']=='file':answer=f'`{path}` is one file, so it has no contained files or folders.'
            return {'path':path,'field':field,'value':value,'answer':answer}
        if self.evidence and name not in {'inspect_path','list_children','summarize_folder','impact_analysis','drive_overview'}:
            arguments.setdefault('scope', self.evidence.result.volume.drive+'\\')
        if name == 'drive_overview':
            if not self.evidence: return {'error':'No scan loaded.'}
            overview=self.evidence.overview()
            overview['allocated_display']=format_bytes(overview['allocated_bytes']) if overview['allocated_bytes'] is not None else 'pending'
            return overview
        if name == 'cleanup_candidates':
            if not self.evidence:
                return {'items':[]}
            items=self.cleanup_suggestions()
            scope=arguments['scope']
            return {'items':[{'path':s.path,'space':format_bytes(s.allocated),'reason':s.reason,'conditions':s.title}
                             for s in items if self.evidence.within(s.path,scope)][:arguments.get('limit',8)],'read_only':True}
        if name == 'find_duplicates':
            return self.index.possible_duplicates(arguments['scope'],arguments.get('limit',5)) if self.index else {'groups':[],'warning':'Index is still being built. No content comparison was performed.'}
        if name == 'find_references':
            name='search_content'
        if name == 'search_files':
            for key,target in [('changed_within_days','modified_after'),('unchanged_for_days','modified_before')]:
                if key in arguments:arguments[target]=int(time.time()-arguments.pop(key)*86400)
            for key,target in [('after_date','modified_after'),('before_date','modified_before')]:
                if key in arguments:
                    arguments[target]=int(datetime.strptime(arguments.pop(key),'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
            if arguments.get('modified_after') and arguments.get('modified_before') and arguments['modified_after']>=arguments['modified_before']:
                raise ValueError('The requested date bounds conflict.')
            if self.index:
                rows=self.index.search_metadata(**arguments)
                if not rows and len(arguments.get('query',''))>4:
                    candidates={}
                    for word in arguments['query'].split()[:4]:
                        if len(word)<4: continue
                        for row in self.index.search_metadata(**{**arguments,'query':word[:4]}):
                            candidates[row['path']]=row
                    return {'items':[self._indexed_item(row) for row in list(candidates.values())[:8]],
                            'match_type':'related name candidates, not exact matches','instruction':'Inspect relevant candidates or ask the user to choose. Do not claim no files exist.'}
                return {'items':[self._indexed_item(row) for row in rows]}
            # ponytail: while indexing, inspect at most 20,000 records and report partial coverage.
            # A completed background index removes this temporary bound.
            matches=[]
            for count,record in enumerate(self.evidence.result.records.values() if self.evidence else []):
                if count>=20000:
                    break
                path=self.evidence.result.path_for(record.frn)
                if not path or not self.evidence.within(path,arguments['scope']): continue
                if arguments.get('query','').casefold() not in record.name.casefold(): continue
                if arguments.get('kind','any') != 'any' and record.is_directory != (arguments['kind']=='folder'): continue
                if arguments.get('extension') and ntpath.splitext(record.name)[1].casefold()!=arguments['extension'].casefold(): continue
                entry=self.evidence._entry(record)
                if arguments.get('minimum_bytes',0) > (entry['allocated_bytes'] or 0): continue
                if arguments.get('modified_before') and (not record.modified or record.modified>=arguments['modified_before']): continue
                if arguments.get('modified_after') and record.modified<arguments['modified_after']: continue
                entry['modified_time']=record.modified or None
                entry['modified_date']=datetime.fromtimestamp(record.modified,timezone.utc).date().isoformat() if record.modified else 'unknown'
                matches.append(entry)
            order=arguments.get('sort','largest')
            if order in {'oldest','recent'}: matches=[row for row in matches if row['modified_time']]
            matches.sort(key=lambda row: row['modified_time'] or 0 if order in {'oldest','recent'} else row.get('allocated_bytes') or 0,reverse=order!='oldest')
            return {'items':matches[:arguments.get('limit',8)],'partial':len(self.evidence.result.records)>20000 if self.evidence else False,'index_status':'building'}

        if name == "inspect_path":
            if not self.evidence: return {"error":"No scan loaded."}
            entry=self.evidence.inspect(arguments['path'])
            if 'error' not in entry:
                entry['tree_evidence']=self.evidence.tree_summary(entry['path'])
                entry['purpose_evidence']=self.evidence.role_evidence(entry['path'])
                entry['known_location']=describe(entry['path'])
                parent=self.evidence.result.records[entry['frn']].parent_frn
                seen={entry['frn']};nearby=[]
                # ponytail: three ancestors and six names per kind, not an unbounded tree dump.
                # The model can inspect another level when this neighbourhood is insufficient.
                for _ in range(3):
                    if parent in seen or parent==self.evidence.result.root_frn or parent not in self.evidence.result.records:break
                    seen.add(parent)
                    children=[self.evidence.result.records[n] for n in self.evidence.result.children.get(parent,())]
                    nearby.append({'path':self.evidence.result.path_for(parent),'total_children':len(children),
                        'files':[r.name for r in children if not r.is_directory][:6],
                        'folders':[r.name for r in children if r.is_directory][:6]})
                    parent=self.evidence.result.records[parent].parent_frn
                if nearby:entry['parent_context']=nearby
            return entry

        if name == "list_children":
            parent=self.evidence.inspect(arguments['path']) if self.evidence else {}
            total=len(self.evidence.result.children.get(parent.get('frn'),())) if self.evidence else 0
            if self.index:
                items=[self._indexed_item(row) for row in self.index.list_children(arguments["path"], limit=self._limit(arguments.get("limit"), 25))]
                return {'items':items,'total_children':total,'partial':len(items)<total}
            if self.evidence:
                scoped = self.evidence.scoped_items(arguments["path"], self._limit(arguments.get("limit"), 8))
                items=scoped.get('items',[])
                return {'items':items,'total_children':total,'partial':len(items)<total}
            return {"items": []}

        if name == "summarize_folder":
            if self.index:
                return self.index.summarize_folder(arguments["path"])
            if self.evidence:
                return self.evidence.folder_summary(arguments["path"])
            return {"error": "No scan loaded."}

        if name == "find_large_files":
            if self.index:
                return {"items": self.index.find_large_files(scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))}
            if self.evidence:
                return self.evidence.largest("files", self._limit(arguments.get("limit"), 8), scope=arguments.get("scope"))
            return {"items": []}

        if name == "search_names":
            if self.index:
                return {"items": self.index.search_names(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))}
            if self.evidence:
                return self.evidence.search(arguments["query"], self._limit(arguments.get("limit"), 8))
            return {"items": []}

        if name == "search_content":
            if not self.index:
                return {"items": [], "warning": "Content index is not available."}
            hits=[{**row,'source':'indexed_text'} for row in self.index.search_content(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))
                  if self.evidence and 'error' not in self.evidence.inspect(row['path']) and self.evidence.within(row['path'],arguments['scope'])]
            checked=0
            if not hits and self.evidence:
                # ponytail: one second / 64 small approved files per lookup, not an exhaustive audit.
                # Narrow the scope or continue for larger searches; never block on a whole-drive text walk.
                deadline=time.monotonic()+1
                for path in self.index.content_candidates(arguments['scope']):
                    if time.monotonic()>=deadline or len(hits)>=arguments.get('limit',8):break
                    if not self.index._safe_for_content_index(path,ntpath.splitext(path)[1].casefold(),False):continue
                    if self.index.include_paths and not any(self.evidence.within(path,p) for p in self.index.include_paths):continue
                    if any(self.evidence.within(path,p) for p in self.index.exclude_paths):continue
                    item=self.evidence.inspect(path)
                    if 'error' in item:continue
                    record=self.evidence.result.records[item['frn']]
                    excerpt=self.evidence._read_excerpt(record,limit=self.index.max_content_bytes);checked+=1
                    text=excerpt.get('text','') if excerpt.get('status')=='text' else ''
                    match=re.search(re.escape(arguments['query']),text,re.I)
                    if match:
                        hits.append({'path':path,'snippet':text[max(0,match.start()-160):match.end()+160],
                                     'source':'current_text_excerpt'})
            return {"items":hits,'scope':arguments['scope'],'additional_files_checked':checked,
                    "coverage":"Only approved bounded text excerpts were searched. No match does not prove the file is unreferenced."}

        if name == "retrieve_context":
            if self.index:
                return self.index.retrieve_context(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))
            return {"items": [], "content_matches": [], "citations": []}

        if name == "impact_analysis":
            if self.index:
                return self.index.impact_analysis(arguments["path"], arguments["action"])
            return {"path": arguments["path"], "action": arguments["action"], "read_only": True, "risk": "unknown"}

        raise ValueError(f"Unsupported tool: {name}")

    def _indexed_item(self,row):
        # Recheck database hits against the active scan, not a previous scan's paths.
        entry=self.evidence.inspect(row['path']) if self.evidence else {'error':'No scan'}
        if 'error' in entry: return {}
        entry['modified_time']=row.get('modified_time') or None
        value=entry['modified_time']
        entry['modified_date']=datetime.fromtimestamp(value,timezone.utc).date().isoformat() if value else 'unknown'
        return entry

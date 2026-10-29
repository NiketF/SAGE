"""Validated read-only conversational tools for drive Q&A."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import ntpath
import os
import sqlite3
import time
import re
from datetime import datetime, timezone
from .known_folders import describe
from .cleanup import find_cleanup_suggestions, CleanupSuggestion
from .evidence import format_bytes

from .local_database import DriveIndex
from .retrieval import ScanEvidence, assess_path

FACT_FIELDS=['size','logical_size','modified','files','folders','items','direct_files','direct_folders','direct_items','drive_capacity']


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
        "list_children": ToolSpec(("path",), ("limit","offset")),
        "summarize_folder": ToolSpec(("path",)),
        "find_large_files": ToolSpec((), ("scope", "limit")),
        "retrieve_context": ToolSpec(("query",), ("scope", "limit")),
        "impact_analysis": ToolSpec(("path", "action")),
        "search_files": ToolSpec((), ("query","scope","kind","extension","minimum_bytes","modified_before","modified_after","changed_within_days","unchanged_for_days","after_date","before_date","sort","limit","offset")),
        "drive_overview": ToolSpec(()),
        "find_duplicates": ToolSpec((), ("scope","limit")),
        "find_references": ToolSpec(("query",), ("scope","limit")),
        "cleanup_candidates": ToolSpec((), ("scope","limit")),
        "report_fact": ToolSpec(("field",),("path","final")),
    }

    def __init__(self, evidence: ScanEvidence | None, index: DriveIndex | None) -> None:
        self.evidence = evidence
        self.index = index
        self.analytics_index = index
        self.focus=[]
        self.subject_scope=[]
        self._uninstall_actions={}
        self._children_cache={}
        self._cleanup_cache=None
        self.scan_changes = None

    def cleanup_suggestions(self):
        if not self.evidence or not self.evidence.result.sizes_complete:return []
        if self._cleanup_cache is None:self._cleanup_cache=find_cleanup_suggestions(self.evidence.result)
        return [s for s in self._cleanup_cache if not self.scan_changes or not self.scan_changes.affected(s.path)]

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
            elif key in {'limit','offset','minimum_bytes','modified_before','modified_after'}:
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
                  'action':{'type':'string'},
                  'field':{'type':'string','enum':FACT_FIELDS},
                  'final':{'type':'boolean','description':'true ONLY if this fact fully answers the question; false if advice or explanation is also needed.'},
                  'limit':{'type':'integer','minimum':1,'maximum':20},
                  'offset':{'type':'integer','minimum':0,'maximum':2**63-1},
                  'kind':{'type':'string','enum':['any','file','folder']},
                  'sort':{'type':'string','enum':['largest','recent','oldest','name']},
                  'extension':{'type':'string','description':'For example .mp4'},
                  'minimum_bytes':{'type':'integer'},'modified_before':{'type':'integer','description':'Unix seconds'},
                  'modified_after':{'type':'integer','description':'Unix seconds'}}
        fields.update({key:{'type':'integer'} for key in ('changed_within_days','unchanged_for_days')})
        fields.update({key:{'type':'string'} for key in ('after_date','before_date')})
        descriptions={
            'report_fact':'Look up size/date/count or drive_capacity. Set final=true ONLY for a pure fact question, never cleanup/impact advice or a ranked-item question. Omit path for one selected item, or for drive_capacity. size=item disk space; logical_size=item data size; drive_capacity=LIVE whole-drive total/used/free/percent, not scanned file totals; modified=last change; counts recursive unless direct_*.',
            'inspect_path':'Inspect a verified item before advice: nested tree, file types, metadata, purpose clues and ownership evidence. Partial coverage never proves safety.',
            'search_files':'Find local files/folders by short subject name, type, size or dates, including when nothing is selected. For rankings use kind=file/folder, sort=largest and the requested limit; never substitute scope totals. Dates use day ranges or ISO after_date/before_date (YYYY-MM-DD, UTC). Never use the whole question as query.',
            'impact_analysis':'Add evidence about a verified item and a proposed action. References are clues, not verified runtime dependencies. System-volume items also include Windows-specific assessment.',
            'list_children':'Page through retained direct children using offset and limit. Follow next_offset for more; inspect returned paths before change advice.',
            'drive_overview':'Get whole-drive counts and size totals.',
            'search_content':'Search approved local text excerpts. Missing results never prove no reference exists.',
            'find_references':'Find text that mentions a filename or identifier. Matches are clues, not verified runtime dependencies.',
            'find_duplicates':'Find possible duplicates by measured size. File contents are not compared.',
            'cleanup_candidates':'Get existing cleanup items in their original order, plus separate optional application uninstall reviews. Never delete application folders directly.'}
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
        changes = self.scan_changes
        if changes and changes.stale:
            path = arguments.get('path') if isinstance(arguments, dict) else None
            if path and changes.is_removed(path):
                return {'error': 'This item was removed after the scan. Refresh to inspect current contents.', 'path': path}
            if name == 'report_fact' and arguments.get('field') != 'drive_capacity':
                path = path or (self.focus[0] if len(self.focus) == 1 else None)
                if path and changes.affected(path):
                    return {'error': 'This scan measurement predates file changes. Refresh for a current measurement.'}
        result = self._call(name, arguments)
        if changes and changes.stale:
            def annotate(value):
                if isinstance(value, list):
                    return [annotate(item) for item in value]
                if isinstance(value, dict):
                    value = {key: annotate(item) for key, item in value.items()}
                    path = value.get('path')
                    if isinstance(path, str) and changes.affected(path):
                        value['scan_item_status'] = 'removed_after_scan' if changes.is_removed(path) else 'changed_after_scan'
                return value
            if not (name == 'report_fact' and arguments.get('field') == 'drive_capacity'):
                result = annotate(result)
            result = dict(result, scan_change_notice=changes.context())
        return result

    def _call(self, name: str, arguments: dict) -> dict:
        self.validate(name, arguments)
        arguments = dict(arguments)
        if name=='report_fact':
            if not self.evidence: return {'error':'No scan loaded.'}
            if arguments['field']=='drive_capacity':
                from ..platform.windows.drives import get_disk_space
                drive=self.evidence.result.volume.drive
                try:total,used,free=get_disk_space(drive)
                except OSError:return {'error':'Windows could not read current capacity. An earlier measurement was not substituted.'}
                if min(total,used,free)<0 or used+free!=total:raise ValueError('Invalid Windows volume measurement.')
                percent=round(100*used/total,1) if total else None
                percent_text=f'{percent:.1f}%' if percent is not None else 'percentage unavailable'
                answer=f"{drive} capacity: {format_bytes(total)}. Used: {format_bytes(used)} ({percent_text}). Free: {format_bytes(free)}. Measured now by Windows."
                return self._record_fact(drive+'\\','drive_capacity',{'total_bytes':total,'used_bytes':used,'free_bytes':free,'used_percent':percent},
                                         answer,source='Windows volume measurement')
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
            if self.evidence.result.skipped_records and field in {'size','logical_size','files','folders','items'}:
                answer+=' These totals cover retained scan records; some records were skipped.'
            if item['kind']!='file' and self.evidence.result.size_errors and field in {'size','logical_size'}:
                answer+=' Some sizes were unavailable, so this total can be lower than actual use.'
            return self._record_fact(path,field,value,answer,source='Retained scan',scan_time=self.evidence.result.scanned_at.isoformat())
        if self.evidence and name not in {'inspect_path','list_children','summarize_folder','impact_analysis','drive_overview'}:
            arguments.setdefault('scope', self.evidence.result.volume.drive+'\\')
        if name == 'drive_overview':
            if not self.evidence: return {'error':'No scan loaded.'}
            overview=self.evidence.overview()
            if self.analytics_index:
                overview=self._record_fact(self.evidence.result.volume.drive+'\\','scan_overview',overview,'',
                    source='Retained scan',scan_time=self.evidence.result.scanned_at.isoformat())['value']
            overview['allocated_display']=format_bytes(overview['allocated_bytes']) if overview['allocated_bytes'] is not None else 'pending'
            return overview
        if name == 'cleanup_candidates':
            if not self.evidence:
                return {'items':[]}
            items=self.cleanup_suggestions()
            scope=arguments['scope']
            return {'items':[{'path':s.path,'space':format_bytes(s.allocated),'reason':s.reason,'conditions':s.title}
                             for s in items if self.evidence.within(s.path,scope)][:arguments.get('limit',8)],'read_only':True,
                    'optional_applications':self._optional_applications(scope,arguments.get('limit',8)),
                    'application_coverage':'Partial native installation inventory; missing records do not establish absence or disuse.'}

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
                limit=arguments.get('limit',8);offset=arguments.get('offset',0)
                rows=self.index.search_metadata(**{**arguments,'limit':limit+1})
                if not rows and not offset and len(arguments.get('query',''))>4:
                    candidates={}
                    for word in arguments['query'].split()[:4]:
                        if len(word)<4: continue
                        for row in self.index.search_metadata(**{**arguments,'query':word[:4]}):
                            candidates[row['path']]=row
                    return {'items':[self._indexed_item(row) for row in list(candidates.values())[:8]],
                            'match_type':'related name candidates, not exact matches','instruction':'Inspect relevant candidates or ask the user to choose. Do not claim no files exist.'}
                more=len(rows)>limit
                items=[self._indexed_item(row) for row in rows[:limit]]
                return {'items':items,'exact_paths':[row['path'] for row in items if row.get('path') and arguments.get('query') and
                    ntpath.basename(row['path']).casefold()==arguments['query'].casefold()],
                    'offset':offset,'next_offset':offset+len(items) if more else None,'partial':bool(offset or more)}
            # ponytail: before indexing completes, metadata search is one O(N)
            # retained-record pass. Normal requests use SQLite once it is ready.
            def candidates():
                for record in self.evidence.result.records.values() if self.evidence else []:
                    if arguments.get('query','').casefold() not in record.name.casefold():continue
                    path=self.evidence.result.path_for(record.frn)
                    if not path or not self.evidence.within(path,arguments['scope']): continue
                    if ntpath.normcase(ntpath.normpath(path))==ntpath.normcase(ntpath.normpath(arguments['scope'])):continue
                    if arguments.get('kind','any') != 'any' and record.is_directory != (arguments['kind']=='folder'): continue
                    if arguments.get('extension') and ntpath.splitext(record.name)[1].casefold()!=arguments['extension'].casefold(): continue
                    entry=self.evidence._entry(record)
                    if arguments.get('minimum_bytes',0) > (entry['allocated_bytes'] or 0): continue
                    if arguments.get('modified_before') and (not record.modified or record.modified>=arguments['modified_before']): continue
                    if arguments.get('modified_after') and record.modified<arguments['modified_after']: continue
                    entry['modified_time']=record.modified or None
                    entry['modified_date']=datetime.fromtimestamp(record.modified,timezone.utc).date().isoformat() if record.modified else 'unknown'
                    yield entry
            order=arguments.get('sort','largest')
            offset=arguments.get('offset',0);limit=arguments.get('limit',8)
            def order_key(row):
                value=row.get('modified_time') or 0 if order in {'oldest','recent'} else row.get('allocated_bytes') or 0
                return (ntpath.basename(row['path']).casefold(),row['path'].casefold()) if order=='name' else (value if order=='oldest' else -value,row['path'].casefold())
            rows=heapq.nsmallest(offset+limit+1,(row for row in candidates() if order not in {'oldest','recent'} or row['modified_time']),key=order_key)
            more=len(rows)>offset+limit
            return {'items':rows[offset:offset+limit],'offset':offset,'next_offset':offset+limit if more else None,
                    'partial':bool(offset or more),'index_status':'building',
                    'coverage':'Searched all retained records. Skipped records, linked contents and live changes remain unknown.'}


        if name == "inspect_path":
            if not self.evidence: return {"error":"No scan loaded."}
            entry=self.evidence.inspect(arguments['path'])
            if 'error' not in entry:
                entry['tree_evidence']=self.evidence.tree_summary(entry['path'])
                entry['tree_access']={'source':'Current retained hierarchy','all_retained_descendants_retrievable':True,
                    'method':'list_children with next_offset; search_files with scope, filters and offset',
                    'limits':'Bounded summaries are partial. Skipped records and unfollowed links are not reconstructed.'}
                if entry['kind']!='file':
                    entry['largest_direct_children']=self.evidence.folder_summary(entry['path']).get('largest_direct_children',[])
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
                assessment=self._windows_assessment(entry)
                if assessment:entry['windows_assessment']=assessment
            return entry

        if name == "list_children":
            if not self.evidence:return {'items':[],'error':'No scan loaded.'}
            parent=self.evidence.inspect(arguments['path'])
            if 'error' in parent:return parent
            if parent.get('is_reparse_point'):return {'items':[],'coverage':'Linked contents were not followed.'}
            frn=parent['frn']
            if frn not in self._children_cache:
                if len(self._children_cache)>=32:self._children_cache.clear()
                self._children_cache[frn]=tuple(sorted(self.evidence.result.children.get(frn,()),
                    key=lambda n:(self.evidence.result.records[n].name.casefold(),n)))
            children=self._children_cache[frn];offset=arguments.get('offset',0);limit=arguments.get('limit',8)
            items=[self.evidence._entry(self.evidence.result.records[n]) for n in children[offset:offset+limit]]
            more=offset+len(items)<len(children)
            return {'items':items,'total_children':len(children),'offset':offset,'next_offset':offset+len(items) if more else None,
                    'partial':bool(offset or more),'coverage':'Page of retained direct children; no live content/dependency check.'}

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
                result=self.index.impact_analysis(arguments["path"], arguments["action"])
            else:result={"path": arguments["path"], "action": arguments["action"], "read_only": True, "risk": "unknown"}
            entry=self.call('inspect_path',{'path':arguments['path']})
            if entry.get('windows_assessment'):result['windows_assessment']=entry['windows_assessment']
            return result

        raise ValueError(f"Unsupported tool: {name}")

    def resolve_subject(self,query,limit=8):
        """Resolve AI-interpreted names without changing general search semantics."""
        if not self.evidence:return {'items':[],'coverage':'No scan loaded.'}
        if ntpath.isabs(query):
            item=self.evidence.inspect(query)
            return {'items':[] if 'error' in item else [item],'partial':False}
        # Multiple conversational targets do not establish one preferred parent.
        scopes=(list(self.subject_scope) if len(self.subject_scope)==1 else [])+[None]
        for scope in scopes:
            if scope:
                current=self.evidence.inspect(scope)
                if 'error' in current:continue
                if current['kind']=='file':scope=ntpath.dirname(scope)
            matches=self.evidence.resolve_name(query,scope,limit=limit+1)
            match_type='exact name'
            if not matches:
                matches=self.evidence.loose_folders(query,scope,limit=limit+1)
                match_type='normalized folder name'
            if matches:
                return {'items':matches[:limit],'partial':len(matches)>limit,'match_type':match_type,
                        'resolution':'conversation scope' if scope else 'retained hierarchy',
                        'instruction':'Inspect the unique target. If alternatives remain, clarify before a change assessment.'}
        return {'items':[],'coverage':'No exact or normalized folder-name match in retained scan records. Skipped or unscanned records remain unknown.'}

    def _optional_applications(self,scope,limit):
        """Advice only: never add app folders to the existing deletion allowlist."""
        apps=[];seen=set()
        windows=os.environ.get('SystemRoot')
        inventory=self.evidence._installed_applications()
        # ponytail: stop optional inventory matching after 40 ms. This is a
        # partial review list, not an exhaustive application/dependency audit.
        deadline=time.monotonic()+.04
        for app in inventory:
            if time.monotonic()>=deadline:break
            path=app['location'];key=ntpath.normcase(path)
            if key in seen or not self.evidence.within(path,scope):continue
            if not app.get('location_verified') or not app.get('uninstall_available') or any(app.get(flag) for flag in ('system_component','no_remove','child_or_update')):continue
            if windows and self.evidence.within(path,windows):continue
            item=self.evidence.inspect(path)
            if 'error' in item or item['kind']!='folder' or item.get('is_reparse_point'):continue
            current=item['frn'];ancestors=set();protected=False
            while current != self.evidence.result.root_frn and current not in ancestors and current in self.evidence.result.records:
                ancestors.add(current);record=self.evidence.result.records[current]
                if record.attributes & 0x400:protected=True;break  # System attribute alone does not establish an OS dependency.
                if current==self.evidence.result.root_frn:break
                current=record.parent_frn
            if protected:continue
            # A shared install root is not one application's removable directory.
            if any(ntpath.normcase(path)==ntpath.normcase(os.environ.get(key,'')) for key in
                   ('ProgramFiles','ProgramFiles(x86)','ProgramData','USERPROFILE','APPDATA','LOCALAPPDATA')):continue
            seen.add(key)
            self._uninstall_actions[key]=CleanupSuggestion(path=item['path'],title=app['name'],
                reason='Optional application uninstall. Review dependencies and preserve needed data first.',
                allocated=item.get('allocated_bytes') or 0,contents_only=False,action='uninstall:'+app['uninstall_string'])
            apps.append({'name':app['name'],'path':item['path'],'publisher':app.get('publisher',''),
                         'action':'review_for_uninstall','method':'Windows Installed apps or the application uninstaller',
                         'conditions':'Only if you no longer need the application. Check dependent work and preserve needed data first.',
                         'limits':'Registration does not verify current use, dependencies or safety. Do not delete its folder directly.'})
            if len(apps)>=limit:break
        return apps

    def _record_fact(self,path,field,value,answer,**metadata):
        index=self.analytics_index or self.index
        if index:
            try:return index.record_fact(path,field,value,answer,**metadata)
            except (OSError,sqlite3.Error) as exc:
                return {'path':path,'field':field,'value':value,'answer':answer,**metadata,'persistence_error':str(exc)}
        return {'path':path,'field':field,'value':value,'answer':answer}

    def _windows_assessment(self,entry):
        """Enrich existing evidence, without overriding benchmark's risk or policy."""
        system_drive=os.environ.get('SystemDrive','C:').rstrip('\\')
        if ntpath.splitdrive(entry['path'])[0].casefold()!=system_drive.casefold():return None
        # Windows supplies these paths. Longest match distinguishes user app data
        # from personal files; drive location alone is not a deletion verdict.
        locations=[(os.environ.get(key),role,method) for key,role,method in (
            ('SystemRoot','Windows-managed area','Windows servicing or Storage cleanup'),
            ('ProgramFiles','Installed-application area','Application uninstaller or Installed apps'),
            ('ProgramFiles(x86)','Installed-application area','Application uninstaller or Installed apps'),
            ('ProgramData','Shared application data','Owning application settings; assess specific contents'),
            ('USERPROFILE','User-profile area','Review the actual personal or application contents'),
            ('APPDATA','User application data','Owning application settings; preserve needed state'),
            ('LOCALAPPDATA','User application data','Owning application settings; preserve needed state'))]
        matches=[row for row in locations if row[0] and self.evidence.within(entry['path'],row[0])]
        match=max(matches,key=lambda row:len(row[0])) if matches else None
        known=entry.get('known_location') or {}
        policy = self._removal_assessment(entry['path'])
        # ponytail: reuse the existing bounded direct-child sample; page the tree
        # for a complete review instead of scanning the drive on every question.
        children = entry.get('largest_direct_children', [])[:6]
        return {'role':match[1] if match else 'Specific role not verified',
                'removal_assessment':policy,
                'child_reviews':[{'path':child['path'], 'category':self._removal_assessment(child['path'])['category']}
                                 for child in children if child.get('path')],
                'review_coverage':'At most six observed direct children; not an exhaustive cleanup or dependency audit.',
                'basis':'Current Windows environment location' if match else 'No native-location match; inspect supplied contents and purpose clues',
                'native_location':match[0] if match else None,
                'supported_method':known.get('advice') or (match[2] if match else 'Establish the item role before a change recommendation'),
                'registered_applications':entry.get('purpose_evidence',{}).get('installation_matches',[]),
                'limits':'Location is not proof of active use, dependency, permissions or deletion safety. Application files are not automatically OS components.',
                'documentation':'https://support.microsoft.com/en-us/windows/experience/storage-filemanagement/free-up-drive-space-in-windows'}

    @staticmethod
    def _removal_assessment(path):
        """Compare scanned paths with the existing sensitive-location reference."""
        reference = path
        for key, role_root in (('SystemRoot', 'Windows'), ('ProgramFiles', 'Program Files'),
                               ('ProgramFiles(x86)', 'Program Files (x86)'), ('ProgramData', 'ProgramData')):
            native = os.environ.get(key)
            if native and ScanEvidence.within(path, native):
                relative = ntpath.relpath(path, native)
                reference = ntpath.join(ntpath.splitdrive(path)[0] + '\\', role_root,
                                        relative if relative != '.' else '')
                break
        guard = assess_path(reference)
        parts = [part for part in ntpath.normpath(reference).casefold().split('\\')[1:] if part]
        first = parts[0] if parts else ''
        critical = (first.startswith('$') and guard['risk'] == 'protected') or first in {'boot', 'recovery', 'system volume information',
                    'pagefile.sys', 'swapfile.sys', 'hiberfil.sys'}
        if first == 'windows' and (guard['risk'] == 'protected' or
                                  len(parts) > 1 and parts[1] in {'system32', 'syswow64', 'winsxs', 'servicing'}):
            critical = True
        container = not parts or (len(parts) == 1 and first in {'users', 'program files', 'program files (x86)', 'programdata'})
        container = container or (first == 'users' and len(parts) == 2)
        app_area = first in {'program files', 'program files (x86)'} or parts[2:5] == ['appdata', 'local', 'programs']
        if critical:
            category = 'system_managed'
        elif container:
            category = 'container_review_children'
        elif app_area:
            category = 'application_removal_review'
        elif guard['risk'] == 'managed_cleanup':
            category = 'managed_cleanup_review'
        elif guard['risk'] == 'review':
            category = 'personal_data_review'
        else:
            category = 'unverified'
        return {'category':category, 'basis':'Known path role; not a permission or dependency check',
                'sensitive_reference_match':critical or container,
                'os_dependency':'Known system role' if critical else 'Not established by location or scan attributes',
                'direct_action_guidance':guard['guidance'],
                'limits':'Unlisted does not mean disposable. Application removal is distinct from deleting its installation files.'}

    def _indexed_item(self,row):
        # Recheck database hits against the active scan, not a previous scan's paths.
        entry=self.evidence.inspect(row['path']) if self.evidence else {'error':'No scan'}
        if 'error' in entry: return {}
        entry['modified_time']=row.get('modified_time') or None
        value=entry['modified_time']
        entry['modified_date']=datetime.fromtimestamp(value,timezone.utc).date().isoformat() if value else 'unknown'
        return entry

"""Record real local-model conversations using synthetic scan and text evidence."""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_drive_conversation import scan
from sage.services.conversation import DriveAssistant,INSTRUCTIONS,PROTOCOL
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.retrieval import ScanEvidence
from sage.services.tool_registry import ToolRegistry

DATA={'Clips/recording.mp4':1000000,'Project/config.json':100,'Project/main.py':200,
      'Packages/tool.msi':500000,'Copies/a.zip':2000,'Copies/b.zip':2000,
      'Toolbox/conda-meta/':0,'Toolbox/envs/':0,'Toolbox/python.exe':3000000}
CASES={
    'media':[('Should I remove this to free space?',(r'E:\Clips',)),('I need these videos for my editing work.',())],
    'size':[('What is its size?',(r'E:\Clips',)),('How much space does it occupy?',())],
    'config':[('What could happen if I remove this?',(r'E:\Project\config.json',))],
    'lookup':[('Find the project configuration file.',())],
    'overview':[('What is using space across this drive?',())],
    'references':[('Which files mention config.json?',())],
    'duplicates':[('Are there any possible duplicate files?',())],
    'environment':[('Would getting rid of this cause problems?',(r'E:\Toolbox',))],
    'paraphrases':[('Can I delete this?',(r'E:\Project\config.json',)),
                   ('Would removing this cause problems?',(r'E:\Project\config.json',)),
                   ('Is this safe to get rid of?',(r'E:\Project\config.json',))],
}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='conversation-review')
    parser.add_argument('--cases',nargs='*',default=list(CASES))
    parser.add_argument('--warm',action='store_true')
    args=parser.parse_args()
    output=ROOT/args.output;output.mkdir(parents=True,exist_ok=True)
    if args.warm:
        OllamaClient().warm([{'role':'system','content':INSTRUCTIONS+PROTOCOL+'\nRead-only tools:\n'+ToolRegistry(None,None).guide()}])
    results=[]
    with tempfile.TemporaryDirectory() as tmp:
        result=scan(DATA)
        for record in result.records.values():record.modified=1700000000
        index=DriveIndex(str(Path(tmp)/'test.sqlite3'))
        def excerpt(record,**kwargs):
            text={'config.json':'{"theme":"dark"}', 'main.py':'settings = read_json("config.json")'}.get(record.name)
            return {'status':'text','text':text} if text else {'status':'not_read'}
        with patch.object(ScanEvidence,'_read_excerpt',side_effect=excerpt):
            index.update_scan(result)
            for name in args.cases:
                assistant=DriveAssistant(result,index=index)
                assistant.tools.index=index;assistant.index_status='ready'
                with patch.object(assistant.evidence,'_installed_applications',return_value=[]):
                    for question,selected in CASES[name]:
                        if name=='paraphrases':
                            assistant.history=[];assistant.focus=[];assistant.listed=[]
                        reply=assistant.answer(question,selected,on_token=lambda _:None)
                        sentences=re.split(r'(?<=[.!?])\s+',re.sub(r'`[^`]*`','PATH',reply))
                        short=sum(len(sentence.split())<=20 for sentence in sentences)
                        record={'case':name,'question':question,'selected':selected,'reply':reply,
                                'timings':assistant.last_timings,'model_metrics':assistant.client.last_metrics,
                                'style_proxy':{'sentences':len(sentences),'at_most_20_words':short,'ratio':round(short/max(1,len(sentences)),3),'formal_STE_certification':False},
                                'lookups':assistant.last_context.get('lookups',[]),'rejected_reply':assistant.last_context.get('rejected_reply')}
                        results.append(record)
                        print(json.dumps(record),flush=True)
                        (output/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    lines=['# Local Qwen conversation checks','','Synthetic MFT records and synthetic approved text. No personal files were scanned or changed. These are model-written replies, except rows explicitly marked fallback.','']
    for record in results:
        lines.extend([f"## {record['case']}",'',record['question'],'',
                      f"{record['timings']['total_seconds']} seconds; {record['timings']['status']}; {record['timings']['model_calls']} model calls.",'',record['reply'],''])
    (output/'RESPONSES.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':main()

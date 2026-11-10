import sys,json
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.metadata_interchange import plan_metadata_import, apply_metadata_import, export_metadata_csv
from src.features.edit.core.commands import ImportMetadataCommand
from src.features.edit.core.smart_bin_rules import validate_rule_tree, matches_rule_tree
from src.features.edit.core.media_pool_query import execute_media_pool_query,MediaPoolQuery
p=EditProject.create('audit').import_asset('/m/a.mov','video',{'fps':25},display_name='Alpha')
aid=next(iter(p.assets)); a=p.assets[aid]
result={}
for text in ('asset_id\n'+aid+'\n','comment\nhello\n'):
    try: result.setdefault('csv_bad_header',[]).append(str(plan_metadata_import(p,text)))
    except Exception as ex: result.setdefault('csv_bad_header',[]).append(type(ex).__name__+': '+str(ex))
p_dup=replace(p, assets={**p.assets,'duplicate':replace(a,asset_id='duplicate',display_name='Duplicate')})
plan=plan_metadata_import(p_dup,'path,comment\n/m/a.mov,intended-target-ambiguous\n')
result['ambiguous_path']={'changes':[c.asset_id for c in plan.asset_changes],'warnings':plan.warnings}
plan=plan_metadata_import(p,'asset_id,comment\n'+aid+',CSV\n')
concurrent=p.update_asset_log_info(aid,comment='concurrent')
cmd=ImportMetadataCommand('audit','Import',plan)
applied=cmd.apply(concurrent); undone=cmd.undo(applied)
result['stale_plan']={'concurrent':concurrent.assets[aid].comment,'after_apply':applied.assets[aid].comment,'after_undo':undone.assets[aid].comment}
kw=p.update_asset_log_info(aid,keywords=('night;exterior','hero'))
roundtripped=apply_metadata_import(kw,plan_metadata_import(kw,export_metadata_csv(kw)))
result['keyword_roundtrip']={'before':kw.assets[aid].keywords,'after':roundtripped.assets[aid].keywords}
tree={'field':'keywords','cmp':'in','value':['Night','Hero']}
validate_rule_tree(tree)
result['smart_keyword_in']=matches_rule_tree(replace(a,keywords=('Night',)),tree)
malformed={'field':'rating','cmp':'>=','value':'not-a-number'}
try:
    validate_rule_tree(malformed)
    result['malformed_numeric_accepted']=matches_rule_tree(a,malformed)
except Exception as ex: result['malformed_numeric_error']=type(ex).__name__+str(ex)
for key in ('reel',):
    try: result['query_'+key]=execute_media_pool_query(p,MediaPoolQuery(sort_key=key)).asset_ids
    except Exception as ex: result['query_'+key]=type(ex).__name__+str(ex)
print(json.dumps(result,ensure_ascii=False,indent=2))

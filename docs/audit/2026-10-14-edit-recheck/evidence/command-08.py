timer.stop()
from src.features.edit.core.edit_project_io import EditProjectIO
io2=EditProjectIO();loaded2=io2.load_project(OUT/'audit-scratch.topos')
old=json.loads((OUT/'timeline_snapshot.json').read_text());new={tid:t.to_dict() for tid,t in loaded2[2].items()}
def diff(a,b,path=''):
    if type(a)!=type(b):return [{'path':path,'before':a,'after':b}]
    if isinstance(a,dict):
        vals=[]
        for k in sorted(set(a)|set(b)):
            if k not in a or k not in b:vals.append({'path':path+'/'+k,'before':a.get(k),'after':b.get(k)})
            else:vals.extend(diff(a[k],b[k],path+'/'+k))
        return vals
    if isinstance(a,list):
        if len(a)!=len(b):return [{'path':path,'before':a,'after':b}]
        return [d for i,(x,y) in enumerate(zip(a,b)) for d in diff(x,y,path+'/'+str(i))]
    return [] if a==b else [{'path':path,'before':a,'after':b}]
diffs=diff(old,new)
(OUT/'save-reload-diff.json').write_text(json.dumps(diffs,ensure_ascii=False,indent=2))
record('save_reload_diff',diffs=diffs)
io2.cleanup();timer.start(500)

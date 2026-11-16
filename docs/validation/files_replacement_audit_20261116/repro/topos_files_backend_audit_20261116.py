"""Read-only repository audit; every filesystem/SQLite mutation is under TemporaryDirectory."""
import ast
import json
import logging
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'external/file_explorer'))
import file_tasks
logging.basicConfig(level=logging.CRITICAL)

def report(case,**fields): print(json.dumps({'case':case,**fields},ensure_ascii=False))
def wait(task):
    deadline=monotonic()+4
    while monotonic()<deadline:
        snap=task.snapshot()
        if snap['status'] in ('completed','failed','cancelled'):return snap
        sleep(.005)
    raise RuntimeError(task.snapshot())

with TemporaryDirectory(prefix='topos-files-audit-', dir='/tmp') as temporary:
    root=Path(temporary)
    for name in ('a','b','out'):(root/name).mkdir()
    a=root/'a'/'same.mov';b=root/'b'/'same.mov'
    a.write_bytes(b'ORIGINAL A');b.write_bytes(b'ORIGINAL B')
    svc=file_tasks.FileTaskService();resolve=svc._resolve_destination;barrier=Barrier(2)
    def raced(item,policy):
        destination=resolve(item,policy);barrier.wait(2);return destination
    svc._resolve_destination=raced
    jobs=[svc.start('move',[str(p)],policy='error',options={'destination':str(root/'out')}) for p in (a,b)]
    snaps=[wait(task) for task in jobs]
    report('concurrent_move_error_policy',statuses=[s['status'] for s in snaps],sources_exist=[a.exists(),b.exists()],only_output=(root/'out/same.mov').read_text())

    for operation in ('copy','move','rename'):
        folder=root/f'self-{operation}';folder.mkdir();source=folder/'source.mov';source.write_text('keep me')
        opts={'new_name':source.name} if operation=='rename' else {'destination':str(folder)}
        snap=wait(file_tasks.FileTaskService().start(operation,[str(source)],policy='overwrite',options=opts))
        report('self_overwrite_'+operation,status=snap['status'],source_exists=source.exists(),content=source.read_text(),failures=snap['failures'])

    folder=root/'descendant';folder.mkdir();(folder/'source.mov').write_text('keep me');child=folder/'child';child.mkdir()
    snap=wait(file_tasks.FileTaskService().start('move',[str(folder)],options={'destination':str(child)}))
    report('move_into_descendant',status=snap['status'],source_exists=(folder/'source.mov').exists(),failures=snap['failures'])

    source=root/'symlink-source.mov';source.write_text('REPLACEMENT');locked=root/'locked';locked.mkdir();secret=locked/'secret.mov';secret.write_text('PROTECTED ORIGINAL');out=root/'symlink-out';out.mkdir();(out/source.name).symlink_to(secret)
    snap=wait(file_tasks.FileTaskService().start('copy',[str(source)],policy='overwrite',options={'destination':str(out)}))
    report('copy_overwrite_follows_destination_symlink',status=snap['status'],target_still_symlink=(out/source.name).is_symlink(),secret_content=secret.read_text())

    # All DB access is redirected before importing the route composition root.
    import db
    database=root/'audit.sqlite'
    connect=lambda:sqlite3.connect(database)
    db._db=connect
    db._resolve_db_file=lambda:str(database)
    db.init_db()
    import main
    import routes
    import trash
    routes._db=connect
    routes.invalidate_cache=lambda path:None
    routes.thumb_engine.get_thumb_path=lambda *a,**kw:str(root/'no-thumbnail')
    trash._db=connect
    trash.invalidate_cache=lambda path:None
    trash_folder=root/'trash';trash_folder.mkdir()
    trash.get_trash_dir=lambda:str(trash_folder)

    con=connect()
    con.execute("INSERT INTO file_meta(path,tags,rating) VALUES('/old.mov','tag',4)")
    con.execute("INSERT INTO ocr_text(file_path,text) VALUES('/old.mov','words')")
    con.execute("INSERT INTO markers(file_path,t_in,label) VALUES('/old.mov',1,'mark')")
    con.commit();con.close()
    routes._move_meta_rows('/old.mov','/new.mov')
    con=connect()
    report('path_migration_columns',file_meta=con.execute('SELECT path FROM file_meta').fetchall(),ocr_text=con.execute('SELECT file_path FROM ocr_text').fetchall(),markers=con.execute('SELECT file_path FROM markers').fetchall())
    con.close()

    f=root/'trash-source.mov';f.write_text('original media')
    con=connect()
    con.execute("INSERT INTO file_meta(path,tags,rating,width,stack_id) VALUES(?, 'tag',5,3840,1)",(str(f),))
    con.execute("INSERT INTO collections(id,name) VALUES(1,'collection')")
    con.execute('INSERT INTO collection_items(collection_id,file_path) VALUES(1,?)',(str(f),))
    con.execute('INSERT INTO stacks(id,cover_path) VALUES(1,?)',(str(f),))
    con.commit();con.close()
    records,errors=trash.trash_items([str(f)])
    f.write_text('different media now occupies original path')
    restored,restore_errors=trash.restore_items([records[0]['id']])
    restored_path=restored[0]['path']
    con=connect()
    report('trash_restore_name_conflict',trash_errors=errors,restore_errors=restore_errors,restored_path=restored_path,restored_meta=con.execute('SELECT path,rating,width,stack_id FROM file_meta WHERE path=?',(restored_path,)).fetchall(),collection_paths=con.execute('SELECT file_path FROM collection_items').fetchall(),stack_cover=con.execute('SELECT cover_path FROM stacks WHERE id=1').fetchone(),original_path_now=f.read_text())
    con.close()

    unlocked=root/'unlocked';unlocked.mkdir()
    con=connect();con.execute("INSERT INTO folder_locks(path,pwd_hash) VALUES(?, 'test')",(str(locked),));con.commit();con.close()
    client=main.app.test_client();headers={'X-Auth-Token':main.APP_TOKEN}
    response=client.post('/api/tasks/start',json={'operation':'mkdir','targets':[str(unlocked)],'options':{'parent':str(locked),'name':'bypassed'}},headers=headers)
    payload=response.get_json();job=payload.get('job')
    if job:
        deadline=monotonic()+3
        while monotonic()<deadline:
            job=client.get('/api/tasks/status',query_string={'job_id':job['job_id']},headers=headers).get_json()['job']
            if job['status'] in ('completed','failed','cancelled'):break
            sleep(.01)
    report('mkdir_parent_lock_bypass',http_status=response.status_code,status=job['status'] if job else None,created=(locked/'bypassed').exists())

    # Search paging using a local DB and mocked stats for 2001 distinct local
    # paths; no 2001 real files or thumbnails are needed to establish slicing.
    con=connect();con.executemany('INSERT INTO file_meta(path,tags,rating,missing) VALUES(?,?,?,0)',[(f'/audit-search/{i:04d}.mov','',0) for i in range(2001)]);con.commit();con.close()
    original_exists=routes.os.path.exists;original_stat=routes.os.stat
    def exists(path): return True if str(path).startswith('/audit-search/') else original_exists(path)
    def stat(path,*args,**kwargs):
        if str(path).startswith('/audit-search/'):return SimpleNamespace(st_size=1,st_mtime=1700000000)
        return original_stat(path,*args,**kwargs)
    routes.os.path.exists=exists;routes.os.stat=stat
    response=client.get('/api/search',query_string={'q':'audit-search','offset':2000,'limit':10},headers=headers)
    payload=response.get_json()
    routes.os.path.exists=original_exists;routes.os.stat=original_stat
    report('search_offset_2000',http_status=response.status_code,total=payload.get('total'),returned=len(payload.get('files',[])),truncated=payload.get('truncated'))

    # Compile the exact method with inert UI collaborators; actual immutable
    # EditProject and actual prepare_import execute their real logic.
    from src.features.edit.core.project import EditProject
    from src.features.files.import_job import ImportCandidate,ImportProbeResult
    source=ast.parse((ROOT/'src/features/edit/ui/timeline_view.py').read_text())
    cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='TimelineView')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_finish_external_import')
    module=ast.Module(body=[method],type_ignores=[])
    namespace={'Any':Any,'logger':logging.getLogger('audit'),'get_media_type':lambda p:'video','MacroCommand':SimpleNamespace(create=lambda *a:object())}
    exec(compile(module,'timeline_view._finish_external_import','exec'),namespace)
    project=EditProject.create('audit')
    track=SimpleNamespace(is_locked=False,name='V1')
    timeline=SimpleNamespace(revision=0,get_track_by_id=lambda p:track)
    published=[];messages=[]
    view=SimpleNamespace(_external_job_id=1,_project=project,_timeline=timeline,
        _external_target={'project':project,'project_id':project.project_id,'sequence_revision':0,'track_id':'V1','create_track':False,'frame':0},
        project_changed=SimpleNamespace(emit=published.append),status_message_requested=SimpleNamespace(emit=messages.append),
        _ensure_first_asset_timeline_settings=lambda a:True,_get_asset_duration_frames=lambda a:25)
    def add(asset,track,frame):view._batch_drop_commands.append(object());return True
    def fail(command):raise ValueError('simulated command rejection')
    view._add_asset_as_clip=add;view._execute_command=fail
    namespace['_finish_external_import'](view,ImportProbeResult(1,(ImportCandidate('/audit.mov','video',True,{'duration_seconds':1}),)))
    report('timeline_failed_macro_publishes_import',assets_before=len(project.assets),assets_after=len(view._project.assets),published_projects=len(published),message=messages[-1])

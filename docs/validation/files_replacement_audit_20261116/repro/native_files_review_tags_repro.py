from pathlib import Path
import ast,sqlite3,types,os
path='/Users/hengshi/Documents/vscode/topos-studio/external/file_explorer/routes.py'
mod=ast.parse(open(path).read());fn=next(n for n in mod.body if isinstance(n,ast.FunctionDef) and n.name=='api_batch_meta');fn.decorator_list=[]
dbpath='/tmp/native-files-review-tags.db'
if os.path.exists(dbpath):os.remove(dbpath)
c=sqlite3.connect(dbpath);c.executescript('CREATE TABLE file_meta(path TEXT PRIMARY KEY,tags TEXT,comments TEXT,rating INTEGER); CREATE TABLE tags(id INTEGER PRIMARY KEY,name TEXT UNIQUE); CREATE TABLE file_tags(file_path TEXT,tag_id INTEGER,UNIQUE(file_path,tag_id));');c.executemany('INSERT INTO file_meta VALUES(?,?,?,?)',[('/a.png','oldA','',0),('/b.png','oldB','',0)]);c.commit();c.close()
env={'request':types.SimpleNamespace(json={'files':['/a.png','/b.png'],'tags':'new'}),'jsonify':lambda obj:obj,'_lock_guard':lambda paths:None,'_db':lambda:sqlite3.connect(dbpath),'_meta_key':lambda path:path}
exec(compile(ast.Module(body=[fn],type_ignores=[]),path,'exec'),env)
print('BATCH_RESPONSE',env['api_batch_meta']());c=sqlite3.connect(dbpath);print('DATABASE_AFTER_NEW',c.execute('SELECT path,tags FROM file_meta ORDER BY path').fetchall());c.close()
env['request'].json['tags']='';print('CLEAR_RESPONSE',env['api_batch_meta']());c=sqlite3.connect(dbpath);print('DATABASE_AFTER_CLEAR',c.execute('SELECT path,tags FROM file_meta ORDER BY path').fetchall());c.close()

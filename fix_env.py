import pathlib  
f = pathlib.Path('card-bot/admin/admin_server.py')  
c = f.read_text('utf-8')  
old = '    if not path.exists():' + chr(10) + '        raise RuntimeError(f\\" 配置文件不存在: -encodedCommand cABhAHQAaAA= "\)'  
new = '    if not path.exists():' + chr(10) + '        example = path.parent / \.env.example\' + chr(10) + '        if example.exists():' + chr(10) + '            path.write_text(example.read_text(encoding=\utf-8\), encoding=\utf-8\)' + chr(10) + '        else:' + chr(10) + '            path.parent.mkdir(parents=True, exist_ok=True)' + chr(10) + '            path.write_text(\\, encoding=\utf-8\)'  
c = c.replace(old, new)  
f.write_text(c, 'utf-8')  
print('Fix applied')  

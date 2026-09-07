import p115
m = [n for n in dir(p115.P115Client) if not n.startswith("_")]
print("SHARE:", [n for n in m if "share" in n.lower()])
print("FS:", [n for n in m if "fs" in n.lower() or "file" in n.lower()][:40])
print("SNAPSHOT?", hasattr(p115.P115Client, "share_snapshot"))
print("P115FileSystem ls?", hasattr(p115.P115FileSystem, "listdir") or hasattr(p115.P115FileSystem, "ls"))

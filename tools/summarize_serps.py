import json,glob,sys
for f in sorted(glob.glob("data/semrush/serp_*.json")):
    d=json.load(open(f)); rows=[r.split(";") for r in d.get("data","").splitlines()[1:]]
    if not rows: print(f, d); continue
    feat=rows[0][-1].split(","); q=f.split("serp_")[1][:-5].replace("_"," ")
    ai=[r[2] for r in rows if r[1]=="52"]; lp=[r[2] for r in rows if r[1]=="3"]; org=[r[2] for r in rows if r[1]=="-1"]
    jd=[(r[0],r[1]) for r in rows if "justindoyle" in r[2]]
    print("##",q,"| units",d["metadata"]["usage"]["api_units"],"| AIO triggered:","52" in feat,"| JDH:",jd or "-")
    print("   AIO cites:",ai); print("   Local pack:",lp); print("   Organic:",org[:10])

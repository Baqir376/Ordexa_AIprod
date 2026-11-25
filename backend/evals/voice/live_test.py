import base64, json, sys, time, urllib.request
API = "http://localhost:8000"
def call(method, path, body=None, tok=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())
tok = call("POST", "/api/v1/auth/login", {"tenant_code": "qa-lab", "username": "admin", "password": "qa-pass-12345"})["access_token"]
clip = sys.argv[1]
audio = base64.b64encode(open(f"clips/{clip}.ogg", "rb").read()).decode()
r = call("POST", "/api/v1/channels/whatsapp/simulate", {"phone": "+923001110102", "text": "", "media_kind": "audio", "media_base64": audio, "media_type": "audio/ogg"}, tok)
oid = r["ai_order"]["id"]
for _ in range(60):
    o = call("GET", f"/api/v1/ai/orders/{oid}", tok=tok)
    if o["status"] not in ("received", "processing"):
        break
    time.sleep(2)
print(o["number"], o["status"])
print("heard:", (o.get("voice") or {}).get("text"), "| clarity:", (o.get("voice") or {}).get("clarity"), "| cleaned:", (o.get("voice") or {}).get("cleaned"))
print("hints:", [(h["heard"], h["product"]) for h in (o.get("voice") or {}).get("hints", [])])
print("lines:", [(l["phrase"], l.get("read_as"), l["product_name"], l["quantity"]) for l in o["lines"]])
print("replies:", r.get("bot_replies"))

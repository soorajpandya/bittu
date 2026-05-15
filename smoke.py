import urllib.request, json
try:
    with urllib.request.urlopen("https://vllqryousoshbfakixup.supabase.co/rest/v1/") as r:
        print("Rest status: " + str(r.status))
except Exception as e:
    print("Rest error: " + str(e))

try:
    supabase_url = 'https://vllqryousoshbfakixup.supabase.co/auth/v1/token?grant_type=password'
    anon_key = 'eyJhbGciOiJIUzI1NiIsImtpZCI6IkdLN1Z2cVBXcHlIN3JmSWQiLCJ0eXAiOiJKV1QifQ.eyJpc3MiOiJodHRwczovL3ZsbHFyeW91c29zaGJmYWtpeHVwLnN1cGFiYXNlLmNvL2F1dGgvdjEiLCJzdWIiOiJhbm9uIiwiYXVkIjoiYW5vbiIsImlhdCI6MTc1OTI4ODg5OCwiZXhwIjoyMDc0ODY0ODk4LCJyb2xlIjoiYW5vbiJ9.a8XjknwRgiGiImtn3-RJV0eTHspRJAElkp5tlB8iVMg'
    auth_payload = json.dumps({'email': 'admin@bittupos.com', 'password': 'Burptech@10102023'}).encode()
    
    # Headers MUST use the exact key provided.
    headers = {
        'apikey': anon_key.strip(),
        'Content-Type': 'application/json'
    }
    
    req = urllib.request.Request(supabase_url, data=auth_payload, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            auth_res = json.loads(response.read().decode())
            token = auth_res['access_token']
            print("Successfully obtained token")
    except urllib.error.HTTPError as he:
        print(f"Auth failed with {he.code}: {he.read().decode()}")
        # If 401, trying without key just in case (unlikely but debugging)
        if he.code == 401:
             try:
                 req2 = urllib.request.Request(supabase_url, data=auth_payload, headers={'Content-Type': 'application/json'})
                 with urllib.request.urlopen(req2) as resp2:
                     print("Auth worked WITHOUT apikey")
                     token = json.loads(resp2.read().decode())['access_token']
             except: pass

    if 'token' not in locals():
         print("Could not get token.")
         exit(1)

    endpoints = ['http://127.0.0.1:8000/api/v1/super-admin/me', 'http://127.0.0.1:8000/api/v1/super-admin/stats', 'http://127.0.0.1:8000/api/v1/super-admin/admins']
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(req) as resp:
                data = resp.read().decode()
                print('URL: ' + url)
                print('Status: ' + str(resp.status))
                print('Body: ' + data[:800] + '\n')
        except Exception as e:
            print('URL: ' + url)
            print('Error: ' + str(e) + '\n')
except Exception as global_e:
    print('Global Error: ' + str(global_e))

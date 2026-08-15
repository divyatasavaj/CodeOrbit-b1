import zipfile
import io
import urllib.request
import json
import time

zip_buffer = io.BytesIO()
with zipfile.ZipFile(zip_buffer, 'w') as zf:
    zf.write('D:/hackorbit/CodeOrbit/demo/sample_legacy.py', 'sample_legacy.py')
zip_bytes = zip_buffer.getvalue()

boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
body = (
    f'--{boundary}\r\n'
    'Content-Disposition: form-data; name="file"; filename="sample_legacy.zip"\r\n'
    'Content-Type: application/zip\r\n\r\n'
).encode('utf-8') + zip_bytes + f'\r\n--{boundary}--\r\n'.encode('utf-8')

req = urllib.request.Request(
    'http://localhost:8000/analyze',
    data=body,
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
)

res = urllib.request.urlopen(req)
data = json.loads(res.read().decode())
print('Upload response:', data)
job_id = data['job_id']

for i in range(30):
    time.sleep(2)
    res = urllib.request.urlopen(f'http://localhost:8000/results/{job_id}')
    result = json.loads(res.read().decode())
    print(f"Status: {result.get('status')}, Progress: {result.get('progress')}")
    if result.get('status') in ['complete', 'error']:
        print('\n--- SUMMARY ---')
        print(json.dumps(result.get('summary', {}), indent=2))
        print('\n--- EXPLANATION SAMPLE ---')
        if result.get('explanation'):
            print(json.dumps(result['explanation'][0], indent=2)[:400])
        break

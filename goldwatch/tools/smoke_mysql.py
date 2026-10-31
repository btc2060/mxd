"""Local disposable Docker smoke test. Does NOT contact 7881 or a user's database."""
from pathlib import Path
import base64
import os
import secrets
import subprocess
import time
import uuid

root = Path(__file__).resolve().parent.parent
suffix = uuid.uuid4().hex[:8]
network, mysql, web = (f"goldwatch-{kind}-{suffix}" for kind in ("testnet", "testdb", "testweb"))
environment = dict(os.environ)
for selector in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
    environment.pop(selector, None)
password, token = secrets.token_hex(24), secrets.token_hex(24)
environment.update(MYSQL_ROOT_PASSWORD=password, MYSQL_DATABASE="goldwatch_test", ADMIN_TOKEN=token,
                   DATABASE_URL=f"mysql+pymysql://root:{password}@mysql:3306/goldwatch_test?charset=utf8mb4")
docker = ["docker", "--host=unix:///var/run/docker.sock"]


def run(*arguments, check=True, input=None):
    return subprocess.run(docker + list(arguments), env=environment, capture_output=True, text=True, check=check, input=input)


def ready(command, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if run(*command, check=False).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Disposable container readiness timeout")


try:
    run("network", "create", network)
    run("run", "-d", "--name", mysql, "--network", network, "--network-alias", "mysql",
        "-e", "MYSQL_ROOT_PASSWORD", "-e", "MYSQL_DATABASE", "mysql:8.4")
    print("Starting disposable MySQL 8.4…", flush=True)
    ready(["exec", mysql, "mysqladmin", "ping", "-h", "127.0.0.1", "--silent"])
    run("run", "-d", "--name", web, "--network", network, "-e", "DATABASE_URL", "-e", "ADMIN_TOKEN",
        "-e", "COLLECTOR_ENABLED=false", "goldwatch:local")
    ready(["exec", web, "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"])
    html = base64.b64encode((root / "tests/fixtures/quotes.html").read_bytes()).decode()
    code = f"""
import asyncio,base64,os
import httpx
from playwright.async_api import async_playwright
client=httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False)
assert client.get('/api/health').json()['ok']
assert client.post('/api/admin/check').status_code==401
response=client.post('/api/import-html',headers={{'Authorization':'Bearer '+os.environ['ADMIN_TOKEN']}},json={{'html':base64.b64decode('{html}').decode()}})
assert response.status_code==200,response.text
assert response.json()['merchant_count']==2 and response.json()['player_count']==5
summary=client.get('/api/summary').json()
assert abs(summary['markets']['merchant']['wan_per_yuan']-18.828)<0.00001
assert abs(summary['markets']['player']['wan_per_yuan']-18.66)<0.00001
assert summary['spread']['yuan_per_wan']>0
assert client.get('/api/export.csv').status_code==200
async def browser_test():
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=os.environ['CHROMIUM_EXECUTABLE_PATH'],headless=True,args=['--disable-dev-shm-usage'])
        page=await browser.new_page()
        await page.goto('http://127.0.0.1:8000',wait_until='networkidle')
        await page.wait_for_function("document.getElementById('merchant-price').textContent!=='—'")
        assert await page.locator('#price-chart canvas').count()==1
        await browser.close()
asyncio.run(browser_test())
print('MySQL persistence, Decimal prices, public/admin API and container Chromium: PASS')
"""
    result = run("exec", "-i", web, "python", "-", input=code)
    print(result.stdout.strip(), flush=True)
    run("restart", "--time", "3", web)
    ready(["exec", web, "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"])
    check = "import httpx;r=httpx.get('http://127.0.0.1:8000/api/quotes?side=player',trust_env=False);assert len(r.json()['rows'])==5;print('Restart persistence: PASS')"
    print(run("exec", web, "python", "-c", check).stdout.strip(), flush=True)
finally:
    run("rm", "-f", "-v", web, check=False)
    run("rm", "-f", "-v", mysql, check=False)
    run("network", "rm", network, check=False)

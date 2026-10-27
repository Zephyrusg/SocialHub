"""Syntax-check review templates in disposable local directories; never deploys."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='socialhub-template-') as tmp:
 root=Path(tmp);(root/'secrets').mkdir();(root/'secrets/runtime.env').write_text('TEMPLATE_ONLY=true\n')
 shutil.copy(ROOT/'compose.production.yml',root/'compose.yml')
 env=os.environ|{'SOCIALHUB_IMAGE':'socialhub@sha256:'+'a'*64}
 result=subprocess.run(['docker','compose','-f',str(root/'compose.yml'),'config','--quiet'],env=env,capture_output=True)
 if result.returncode:raise SystemExit('Production Compose template syntax failed')
 # These are explicit stand-ins for future existing Pi snippets, NOT copied production files.
 snippets=root/'snippets';snippets.mkdir()
 (snippets/'ssl-common.conf').write_text('ssl_certificate /local-ca/ca.crt;\nssl_certificate_key /local-ca/tls.key;\n')
 (snippets/'trusted-origin.conf').write_text('listen 8088;\n')
 shutil.copy(ROOT/'deployment/production/socialhub.example.test.conf',root/'vhost.conf')
 (root/'nginx.conf').write_text('events {}\nhttp {map $server_port $external_scheme {default https;} include /etc/nginx/socialhub-vhost.conf;}\n')
 args=['docker','run','--rm','--network','none','--entrypoint','nginx',
  '-v',str(root/'nginx.conf')+':/etc/nginx/nginx.conf:ro',
  '-v',str(root/'vhost.conf')+':/etc/nginx/socialhub-vhost.conf:ro',
  '-v',str(snippets)+':/etc/nginx/snippets:ro',
  '-v',str(ROOT/'secrets/local-oidc/ca.crt')+':/local-ca/ca.crt:ro',
  '-v',str(ROOT/'secrets/local-oidc/tls.key')+':/local-ca/tls.key:ro','nginx:stable','-t']
 result=subprocess.run(args,capture_output=True)
 if result.returncode:raise SystemExit('Dedicated vhost syntax failed with synthetic snippet stand-ins')
print('Production Compose and dedicated vhost syntax PASS (local stand-ins; actual Pi snippets still need approved validation).')

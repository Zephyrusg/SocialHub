"""Enroll only synthetic fixture users; capture CLI-generated secrets privately."""
from pathlib import Path
import json
import re
import subprocess
import yaml
ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'secrets/local-oidc'
COMPOSE=['docker','compose','--project-name','socialhub-oidc-test','-f',str(ROOT/'deployment/local-oidc/compose.yml')]

def main():
 p=DEST/'test-credentials.json';credentials=json.loads(p.read_text());seeds=credentials.setdefault('totp',{})
 for user in ('owner','outsider'):
  if user in seeds:continue
  result=subprocess.run(COMPOSE+['exec','-T','authelia','authelia','storage','user','totp','generate',user,
   '--config','/config/configuration.yml','--force'],capture_output=True,text=True)
  candidates=re.findall(r'\b[A-Z2-7]{32,}\b',result.stdout)
  if result.returncode or len(candidates)!=1:
   raise SystemExit('Synthetic TOTP enrollment failed; credential-bearing CLI output was suppressed')
  seeds[user]=candidates[0]
 p.write_text(json.dumps(credentials));p.chmod(0o600)
 config=DEST/'configuration.yml';data=yaml.safe_load(config.read_text())
 for client in data['identity_providers']['oidc']['clients']:client['authorization_policy']='two_factor'
 config.write_text(yaml.safe_dump(data,sort_keys=False))
 env=DEST/'socialhub.env';env.write_text(env.read_text().replace('OWNER_OIDC_REQUIRE_MFA=false','OWNER_OIDC_REQUIRE_MFA=true'))
 result=subprocess.run(COMPOSE+['restart','authelia'],capture_output=True)
 if result.returncode:raise SystemExit('Synthetic provider MFA policy restart failed')
 print('Synthetic TOTP users enrolled; both local clients now require two factors. No secrets displayed.')

if __name__=='__main__':main()

"""Isolated source-distribution checks; no HOI4 or model calls."""
import hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class DistributionTests(unittest.TestCase):
 def test_identity(self):
  data=json.loads((ROOT/'PUBLICATION.json').read_text())
  self.assertEqual(data['version'],'0.9.0-beta.4')
  self.assertEqual(data['author'],'NightPulse')
 def test_notices(self):
  for name in ('LICENSE','NOTICE','CREDITS.md','ATTRIBUTION.md','THIRD_PARTY_NOTICES.md'):
   self.assertTrue((ROOT/name).is_file(),name)
  self.assertIn('NightPulse',(ROOT/'NOTICE').read_text())
 def test_source_manifest(self):
  data=json.loads((ROOT/'SOURCE-MANIFEST.json').read_text())
  for name,item in data['files'].items():
   self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),item['sha256'],name)
 def test_no_player_files(self):
  data=json.loads((ROOT/'SOURCE-MANIFEST.json').read_text())
  for name in data['files']:
   self.assertFalse(name.endswith(('.hoi4','.sqlite','.sqlite3','.db','.token','.log')),name)
 def test_launchers(self):
  for name in ('start.bat','setup_windows.bat'):
   raw=(ROOT/name).read_bytes()
   self.assertNotIn(b'\n',raw.replace(b'\r\n',b''))
 def test_preflight(self):
  with tempfile.TemporaryDirectory(prefix='eyes public test ') as tmp:
   env=dict(os.environ,EYES_PROFILE_DIR=tmp,PYTHONIOENCODING='utf-8')
   result=subprocess.run([sys.executable,str(ROOT/'tools/release_preflight.py')],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual(list(Path(tmp).iterdir()),[])
if __name__=='__main__':unittest.main()

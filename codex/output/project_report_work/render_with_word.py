"""Use the skill renderer with Word PDF export on Windows without bundled LO."""
from pathlib import Path
import importlib.util
import subprocess
import sys

SKILL = Path('C:/Users/rs/.codex/plugins/cache/openai-primary-runtime/documents/26.1025.11240/skills/documents')
spec=importlib.util.spec_from_file_location('document_renderer',SKILL/'render_docx.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def convert(doc_path,user_profile,convert_tmp_dir,stem,verbose=False):
    pdf=Path(convert_tmp_dir)/(Path(doc_path).stem+'.pdf')
    def literal(p):return "'"+str(Path(p).resolve()).replace("'","''")+"'"
    code=f'''$taskWord=New-Object -ComObject Word.Application
$taskWord.Visible=$false
$taskWord.DisplayAlerts=0
try {{
 $taskDoc=$taskWord.Documents.Open({literal(doc_path)},$false,$true)
 $taskDoc.Repaginate()
 $taskDoc.ExportAsFixedFormat({literal(pdf)},17)
 $taskDoc.Close(0)
}} finally {{ $taskWord.Quit() }}'''
    import base64
    encoded=base64.b64encode(code.encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell.exe','-NoProfile','-EncodedCommand',encoded],check=True,timeout=90,capture_output=True)
    return str(pdf),'Word 16 native PDF export'

m.convert_to_pdf=convert
if __name__=='__main__':m.main()

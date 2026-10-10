"""Optional Windows.Media.Ocr calibration experiment, never imported by the detector.

Requires a locally installed Windows SDK/.NET reference assemblies and an OCR
language (Italian for the current templates). Installs nothing; no GUI automation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from analysis.gameplay.cache import atomic_write_json
from export.output_safety import ensure_output_is_safe
from utils.subprocess_utils import run_no_window


def recognized_result(text: str) -> str:
    letters = re.sub('[^a-z]', '', unicodedata.normalize('NFKD', text).casefold())
    victory, defeat = 'vittoria' in letters, 'sconfitta' in letters
    return 'VICTORY' if victory and not defeat else 'DEFEAT' if defeat and not victory else 'UNKNOWN'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if os.name != 'nt':
            raise ValueError('Windows.Media.Ocr experiment is available only on Windows')
        manifest = args.manifest.expanduser().resolve()
        entries = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(entries, list) or len(entries) > 20000:
            raise ValueError('Expected a bounded image manifest array')
        protected = [manifest, *(Path(entry['path']) for entry in entries)]
        destination = ensure_output_is_safe(args.output, protected)
        windows = Path(os.environ.get('WINDIR', r'C:\Windows'))
        framework = windows/'Microsoft.NET'/'Framework64'/'v4.0.30319'
        sdk = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))/'Windows Kits'/'10'/'UnionMetadata'
        metadata = sorted((file for file in sdk.glob('*/Windows.winmd')
                           if re.fullmatch(r'\d+(?:\.\d+)+', file.parent.name)),
                          key=lambda file: tuple(int(part) for part in file.parent.name.split('.')))
        runtime = sorted((windows/'Microsoft.Net'/'assembly'/'GAC_MSIL'/'System.Runtime.WindowsRuntime').glob('*/System.Runtime.WindowsRuntime.dll'))
        compiler = framework/'csc.exe'
        if not compiler.is_file() or not metadata or not runtime:
            raise ValueError('Optional experiment requires Windows SDK and .NET reference assemblies; core detector does not')
        references = [framework/'System.Runtime.dll', runtime[-1], framework/'System.Threading.Tasks.dll',
                      framework/'System.Runtime.InteropServices.WindowsRuntime.dll', metadata[-1]]
        source = Path(__file__).with_name('WindowsOcrExperiment.cs')
        with tempfile.TemporaryDirectory(prefix='recut-ocr-experiment-') as temporary:
            executable = Path(temporary)/'OcrExperiment.exe'
            compiled = run_no_window([str(compiler), '/nologo', '/target:exe', '/out:'+str(executable),
                                     '/reference:System.Web.Extensions.dll',
                                     *('/reference:'+str(file) for file in references), str(source)],
                                    capture_output=True, text=True, timeout=60)
            if compiled.returncode:
                raise RuntimeError(compiled.stdout + compiled.stderr)
            measured = run_no_window([str(executable), str(manifest)], capture_output=True, text=True,
                                     encoding='utf-8', timeout=900)
            if measured.returncode:
                raise RuntimeError(measured.stderr)
            report = json.loads(measured.stdout)
        by_id = {str(entry['id']): entry for entry in entries}
        for row in report['rows']:
            row.update({key:value for key,value in by_id[row['id']].items() if key not in ('id','path')})
            row['prediction'] = recognized_result(row['text'])
        report['role'] = 'calibration_experiment_only'
        report['normalization'] = 'Unicode decomposition, letters only, exact Italian target words; both words -> UNKNOWN'
        report['runtime_dependency_added'] = False
        atomic_write_json(destination, report)
        print(destination)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())

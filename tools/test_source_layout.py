"""Repository inputs must be available to package, upstream and image builders."""
import json
from pathlib import Path
import re
import sys
import unittest

import rungic_package

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/ci'))
import build_host_seed


class SourceLayoutTests(unittest.TestCase):
    # covers: delivery.packaging/E2
    def test_declared_package_inputs_and_overlays_exist(self):
        for pkg in rungic_package.definitions().values():
            for name in rungic_package.identity_paths(pkg):
                with self.subTest(package=pkg['name'], source=name):
                    self.assertTrue((ROOT / name).exists(), name)
        for recipe in (ROOT / 'packages').glob('*/recipe.json'):
            for value in json.loads(recipe.read_text()).get('overlay', {}).values():
                source = value if isinstance(value, str) else value['from']
                with self.subTest(recipe=recipe.name, source=source):
                    self.assertTrue((ROOT / source).is_file(), source)

    # covers: delivery.packaging/E2
    def test_build_script_literal_inputs_are_declared(self):
        for pkg in rungic_package.definitions().values():
            script = (pkg['dir'] / 'build.sh').read_text()
            for source in re.findall(r'\$SRC["\x27]?/([^\s"\x27;\\]+)', script):
                # Build outputs live in the staged source, rather than in the repository inputs.
                if '$' in source or 'build' in Path(source).parts or source.startswith(('.work/', 'upstream/')):
                    continue
                with self.subTest(package=pkg['name'], source=source):
                    matches = list(ROOT.glob(source))
                    self.assertTrue(matches, source)
                    for path in matches:
                        self.assertTrue(any(path == ROOT / entry or path.is_relative_to(ROOT / entry)
                                            or (ROOT / entry).is_relative_to(path)
                                            for entry in pkg['paths']), f'undeclared input: {path}')

    # covers: delivery.release-deploy/E7
    def test_incremental_release_and_host_seed_agree(self):
        destinations = {(source, "/data/adb/" + dest): mode for source, dest, mode in build_host_seed.ANDROID_FILES}
        release = json.loads((ROOT / 'release/packages.json').read_text())
        for entry in release['android']:
            with self.subTest(source=entry['source']):
                self.assertTrue((ROOT / entry['source']).is_file())
                mode = destinations[(entry['source'], entry['path'])]
                self.assertEqual(int(str(entry['mode']), 8), mode)


if __name__ == '__main__':
    unittest.main()

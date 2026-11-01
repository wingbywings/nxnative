"""Compaction and the following start use fresh processes and disk-built shaders."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(__name__ == '__main__', 'run test_pso_archive.py with the native t_pso_archive path')
class ArchiveRestartTests(unittest.TestCase):
    def test_compaction_with_fresh_bases_and_restart(self):
        for constants in (False, True):
            for bounded in (False, True):
                with self.subTest(constants=constants, bounded=bounded), tempfile.TemporaryDirectory(prefix='pso-restart-') as folder:
                    env = dict(os.environ, DM_PSO_MTL4='1', MTL_DEBUG_LAYER='1', DM_TEST_ARCHIVE_MIXED='1')
                    for name in ('DM_GPU_CAPTURE', 'MTL_CAPTURE_ENABLED', 'DM_TEST_ARCHIVE_CONSTANTS'):
                        env.pop(name, None)
                    if bounded:
                        env["DM_TEST_ARCHIVE_BATCH"] = "1"
                    if constants:
                        env['DM_TEST_ARCHIVE_CONSTANTS'] = '1'
                    for phase, files in (('seed', 9), ('replay', 5 if bounded else 1), ('verify', 5 if bounded else 1)):
                        result = subprocess.run([executable, '--compact-' + phase, folder],
                                                env=env, capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, phase + '\n' + result.stdout + result.stderr)
                        self.assertEqual(len(list(Path(folder).glob('*.mtl4a'))), files, phase)
                        self.assertEqual((Path(folder) / 'compacting').exists(), phase == 'seed', phase)

    def test_legacy_files_are_preserved_without_repeated_compaction(self):
        with tempfile.TemporaryDirectory(prefix='pso-legacy-') as folder:
            env = dict(os.environ, DM_PSO_MTL4='1', MTL_DEBUG_LAYER='1',
                       DM_TEST_ARCHIVE_MIXED='1', DM_TEST_ARCHIVE_LEGACY='1')
            for phase, count in (('seed', 9), ('replay', 10), ('verify', 10)):
                result = subprocess.run([executable, '--compact-' + phase, folder],
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, phase + '\n' + result.stdout + result.stderr)
                self.assertEqual(len(list(Path(folder).glob('*.mtl4a'))), count)
                if phase == 'seed':
                    for path in Path(folder).glob('*.count'):
                        path.unlink()

    def test_failed_shard_keeps_originals_across_restart(self):
        with tempfile.TemporaryDirectory(prefix='pso-failed-shard-') as folder:
            env = dict(os.environ, DM_PSO_MTL4='1', MTL_DEBUG_LAYER='1',
                       DM_TEST_ARCHIVE_MIXED='1', DM_TEST_ARCHIVE_BATCH='1', DM_TEST_ARCHIVE_FAIL='1')
            for phase, count in (('seed', 9), ('fail', 12), ('recover', 12)):
                result = subprocess.run([executable, '--compact-' + phase, folder],
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, phase + '\n' + result.stdout + result.stderr)
                self.assertEqual(len(list(Path(folder).glob('*.mtl4a'))), count)
                self.assertTrue((Path(folder) / 'compacting').exists())


if __name__ == '__main__':
    executable = sys.argv.pop(1)
    unittest.main()

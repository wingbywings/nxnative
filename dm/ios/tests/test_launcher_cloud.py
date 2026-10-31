"""Exercise the production Swift declaration and asynchronous Objective-C bridge."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == 'darwin', 'requires Apple Swift and Foundation')
class LauncherCloudBridge(unittest.TestCase):
    def test_completion_can_escape_and_returns_on_main_queue(self):
        source = (ROOT / 'dm/ios/Sources/Launcher.swift').read_text()
        declaration = re.search(r'@_silgen_name\("dm_launcher_cloud_transfer"\)\nprivate func cloudTransfer[^\n]+', source).group()
        with tempfile.TemporaryDirectory(prefix='launcher-cloud-') as folder:
            work = Path(folder)
            # Replace only the external cloud transport. The real bridge still
            # dispatches to its worker, retains completion, and dispatches back.
            (work / 'stub.m').write_text('''#import <Foundation/Foundation.h>
BOOL dm_cloud_backup_container(NSError **error) { (void)error; return YES; }
BOOL dm_cloud_restore_container(NSError **error) {
    *error = [NSError errorWithDomain:@"fixture" code:1 userInfo:@{NSLocalizedDescriptionKey:@"No iCloud entitlement"}];
    return NO;
}
''')
            (work / 'main.swift').write_text('''import Foundation
import Dispatch
''' + declaration + '''
let queueKey = DispatchSpecificKey<Int>()
DispatchQueue.main.setSpecific(key: queueKey, value: 1)
func check(_ restoring: Int32) {
    let capture = UUID().uuidString
    cloudTransfer(restoring) { message in
        precondition(!capture.isEmpty)
        precondition(DispatchQueue.getSpecific(key: queueKey) == 1)
        let text = message as String
        if restoring == 0 {
            precondition(text.contains("submitted to iCloud Drive"))
            check(1)
        } else {
            precondition(text.contains("No iCloud entitlement"))
            precondition(text.contains("still play using local saves"))
            print("async backup and unavailable-iCloud completion passed")
            exit(0)
        }
    }
}
check(0)
dispatchMain()
''')
            def run(*args):
                return subprocess.run(args, check=True, capture_output=True, text=True, timeout=90)
            for source, obj in ((ROOT / 'dm/ios/Sources/CloudSaveGate.m', 'bridge.o'), (work / 'stub.m', 'stub.o')):
                run('xcrun', 'clang', '-fobjc-arc', '-I' + str(ROOT / 'dm/host'), '-c', str(source), '-o', str(work / obj))
            run('xcrun', 'swiftc', '-O', '-module-cache-path', str(work / 'modules'), str(work / 'main.swift'), str(work / 'bridge.o'), str(work / 'stub.o'), '-o', str(work / 'test'))
            result = subprocess.run([str(work / 'test')], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('completion passed', result.stdout)


if __name__ == '__main__':
    unittest.main()

"""The runtime container, signing request and packaged requirements must agree."""
import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'tools/gen_icloud.py'
spec = importlib.util.spec_from_file_location('gen_icloud', SOURCE)
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gen = Path(self.temp.name)
        (self.gen / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'org.example.game', 'DMLanguage': 'en-US'}))

    def test_matching_default_container_and_public_save_folder(self):
        selected = cloud.configure(self.gen, 'org.example.game', 'Test Game')
        info = plistlib.loads((self.gen / 'Info.plist').read_bytes())
        requested = plistlib.loads((self.gen / 'DmIOS.entitlements').read_bytes())
        self.assertEqual(selected, 'iCloud.com.oops.game')
        self.assertEqual(info['DMICloudContainerIdentifier'], selected)
        self.assertEqual(info['DMLanguage'], 'en-US')
        self.assertEqual(requested['com.apple.developer.icloud-services'], ['CloudDocuments'])
        for key in ('com.apple.developer.icloud-container-identifiers', 'com.apple.developer.ubiquity-container-identifiers'):
            self.assertEqual(requested[key], [selected])
        self.assertTrue(info['NSUbiquitousContainers'][selected]['NSUbiquitousContainerIsDocumentScopePublic'])
        self.assertEqual((self.gen / 'dm/iCloud.entitlements').read_bytes(), (self.gen / 'DmIOS.entitlements').read_bytes())

    def test_custom_container_preserves_unrelated_signing_requirements(self):
        base = {'com.apple.developer.icloud-services': ['CloudKit'],
                'com.apple.developer.ubiquity-container-identifiers': ['iCloud.org.example.existing'],
                'keychain-access-groups': ['TEAM.org.example.game']}
        cloud.configure(self.gen, 'org.example.game', 'Test', 'iCloud.org.example.shared', base)
        requested = plistlib.loads((self.gen / 'DmIOS.entitlements').read_bytes())
        self.assertEqual(requested['keychain-access-groups'], base['keychain-access-groups'])
        self.assertEqual(requested['com.apple.developer.icloud-services'], ['CloudDocuments', 'CloudKit'])
        self.assertEqual(requested['com.apple.developer.ubiquity-container-identifiers'], ['iCloud.org.example.shared', 'iCloud.org.example.existing'])
        self.assertNotIn('com.apple.developer.kernel.increased-memory-limit', requested)
        self.assertEqual(base['com.apple.developer.icloud-services'], ['CloudKit'])

    def test_invalid_identifier_or_signing_input_does_not_write_output(self):
        for identifier in ('iCloud.*', '../cloud', 'iCloud.', 'iCloud.a/b'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                cloud.configure(self.gen, 'org.example.game', 'Test', identifier)
        with self.assertRaises(ValueError):
            cloud.configure(self.gen, 'org.example.game', 'Test', base={'com.apple.developer.icloud-services': 'CloudDocuments'})
        self.assertFalse((self.gen / 'DmIOS.entitlements').exists())

    def test_capability_is_a_native_dictionary_and_preserves_target_attributes(self):
        project = self.gen / 'fixture.pbxproj'
        data = {'rootObject': 'ROOT', 'objects': {
            'ROOT': {'targets': ['APP'], 'attributes': {'TargetAttributes': {'APP': {'DevelopmentTeam': 'EXAMPLE'}}}},
            'APP': {'name': 'DmIOS'},
        }}
        project.write_bytes(plistlib.dumps(data))
        cloud.enable_capability(project)
        attrs = plistlib.loads(project.read_bytes())['objects']['ROOT']['attributes']['TargetAttributes']['APP']
        self.assertEqual(attrs['DevelopmentTeam'], 'EXAMPLE')
        self.assertEqual(attrs['SystemCapabilities'], {'com.apple.iCloud': {'enabled': 1}})


if __name__ == '__main__':
    unittest.main()

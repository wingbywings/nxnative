// Local transport exercises the production snapshot, journal and conflict logic
// without an iCloud account. Real iCloud metadata/network needs device testing.
#import "../cloud_save.m"

@interface LocalCloud : DMCloudSaveStore
@property BOOL unavailable;
@property BOOL uploadPending;
@property (copy) void (^duringCheck)(void);
@end
@implementation LocalCloud
- (BOOL)checkCloud:(NSURL *)url uploaded:(BOOL)uploaded error:(NSError **)error {
    (void)url; (void)uploaded;
    if (self.duringCheck) { void (^action)(void) = self.duringCheck; self.duringCheck = nil; action(); }
    if (uploaded && self.uploadPending) return waiting(error, @"Injected asynchronous upload");
    return self.unavailable ? fail(error, @"Injected unavailable cloud") : YES;
}
@end
static int failures;
#define CHECK(expr, message) do { if (!(expr)) { fprintf(stderr, "FAIL: %s (line %d)\n", message, __LINE__); failures++; } } while (0)
static void put(NSURL *root, NSString *name, NSString *value) {
    CHECK([value writeToURL:child(root, name) atomically:YES encoding:NSUTF8StringEncoding error:NULL], "write test save");
}
static NSString *get(NSURL *root, NSString *name) { return [NSString stringWithContentsOfURL:child(root, name) encoding:NSUTF8StringEncoding error:NULL]; }
static LocalCloud *device(NSURL *cloud, NSURL *local) { return [[LocalCloud alloc] initWithFolder:cloud local:local title:@"010015100B514000"]; }
int main(void) {
    @autoreleasepool {
        NSFileManager *fm = NSFileManager.defaultManager;
        NSURL *root = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString] isDirectory:YES];
        NSURL *cloud = child(root, @"cloud"), *a = child(root, @"device-a"), *b = child(root, @"device-b");
        for (NSURL *u in @[cloud, a, b]) CHECK([fm createDirectoryAtURL:u withIntermediateDirectories:YES attributes:nil error:NULL], "create fixture");
        put(a, @"progress", @"initial");
        NSError *registrationError;
        DMCloudSaveStore *unregistered = [[DMCloudSaveStore alloc] initWithFolder:cloud local:a title:@"save"];
        CHECK(![unregistered checkCloud:child(a, @"progress") uploaded:NO error:&registrationError] &&
            [registrationError.localizedDescription containsString:@"Complete a backup on the source device"],
            "restore does not promise registration for a non-cloud file");
        CHECK([fm createDirectoryAtURL:child(a, @"empty") withIntermediateDirectories:NO attributes:nil error:NULL], "empty directory");
        put(a, @".dm-case-1", @"case data");
        CHECK(!setxattr(child(a, @".dm-case-1").fileSystemRepresentation, GuestName.UTF8String, "PROGRESS", 8, 0, 0), "guest-name xattr");
        NSError *error;
        LocalCloud *da = device(cloud, a);
        CHECK([da prepare:&error], "initial migration publishes local progress");
        if (error) fprintf(stderr, "initial error: %s\n", error.description.UTF8String);
        LocalCloud *db = device(cloud, b);
        CHECK([db prepare:&error] && [get(b, @"progress") isEqual:@"initial"], "second device downloads complete save");
        if (error) fprintf(stderr, "import error: %s\n", error.description.UTF8String);
        char guest[32] = {0};
        CHECK(getxattr(child(b, @".dm-case-1").fileSystemRepresentation, GuestName.UTF8String, guest, sizeof guest, 0, 0) == 8 && !strcmp(guest, "PROGRESS"), "case metadata survives cloud round trip");
        BOOL isDir = NO; CHECK([fm fileExistsAtPath:child(b, @"empty").path isDirectory:&isDir] && isDir, "empty directory survives");

        put(a, @"progress", @"new-a");
        CHECK([da capture:&error] && [da publish:&error], "successful commit publishes snapshot");
        // B was already playing an older save: never overwrite A's progress.
        put(b, @"progress", @"concurrent-b");
        CHECK([db capture:&error] && ![db publish:&error], "concurrent device write is refused");
        CHECK([get(b, @"progress") isEqual:@"concurrent-b"], "conflicting local progress retained");
        CHECK(![device(cloud, b) prepare:&error], "pending conflict blocks next launch");

        NSURL *c = child(root, @"device-c");
        CHECK([fm createDirectoryAtURL:c withIntermediateDirectories:NO attributes:nil error:NULL], "third device");
        put(c, @"progress", @"old-device-save");
        LocalCloud *dc = device(cloud, c);
        CHECK([dc prepare:&error] && [get(c, @"progress") isEqual:@"new-a"], "first cloud import wins over old local save");
        BOOL backup = NO;
        for (NSString *name in [fm contentsOfDirectoryAtPath:root.path error:NULL])
            if ([name hasPrefix:@"device-c.before-icloud-"]) backup |= [get(child(root, name), @"progress") isEqual:@"old-device-save"];
        CHECK(backup, "existing local save backed up before first cloud import");
        put(c, @"progress", @"pending-c");
        CHECK([dc capture:&error], "durable pending commit");
        dc.unavailable = YES;
        CHECK(![dc publish:&error], "download unavailable blocks publishing");
        dc = device(cloud, c);
        CHECK([dc prepare:&error], "relaunch recovers durable pending commit");
        CHECK([device(cloud, a) prepare:&error] && [get(a, @"progress") isEqual:@"pending-c"], "existing device advances to descendant revision");

        // Simulate death between cloud write and local journal update.
        put(c, @"progress", @"after-crash");
        CHECK([dc capture:&error], "capture before crash fixture");
        NSURL *journalURL = [NSURL fileURLWithPath:[c.path stringByAppendingString:@".icloud-state.plist"]];
        NSData *pendingJournal = [NSData dataWithContentsOfURL:journalURL];
        CHECK([dc publish:&error] && [pendingJournal writeToURL:journalURL atomically:YES], "simulate lost publish acknowledgement");
        CHECK([device(cloud, c) prepare:&error], "publish acknowledgement recovery is idempotent");

        // Another game commit arrives while the previous one is uploading,
        // then the process dies after that upload but before its acknowledgement.
        dc = device(cloud, c); CHECK([dc prepare:&error], "resume before overlapping commits");
        put(c, @"progress", @"uploading-c"); CHECK([dc capture:&error], "capture older overlapping commit");
        __block NSData *inFlightJournal;
        __weak LocalCloud *weakDevice = dc;
        dc.duringCheck = ^{
            put(c, @"progress", @"latest-c"); CHECK([weakDevice capture:NULL], "capture newer commit during cloud access");
            inFlightJournal = [NSData dataWithContentsOfURL:journalURL];
        };
        CHECK(![dc publish:&error] && error.code == 2, "newer queued commit is not reported as fully synchronized");
        CHECK([inFlightJournal writeToURL:journalURL atomically:YES], "restore interrupted upload journal");
        dc = device(cloud, c);
        CHECK(![dc prepare:&error] && error.code == 2, "acknowledge interrupted older upload without losing newer progress");
        CHECK([dc prepare:&error] && [get(c, @"progress") isEqual:@"latest-c"], "retry publishes newest pending commit after recovery");
        put(c, @"progress", @"commit-before-outbox-crash");
        CHECK(![device(cloud, c) prepare:&error] && [get(c, @"progress") isEqual:@"commit-before-outbox-crash"], "unrecorded local commit is preserved and blocks import");

        NSURL *remoteURL = child(cloud, @"010015100B514000.nxsave");
        NSMutableDictionary *bad = [readPlist(remoteURL, &error) mutableCopy];
        bad[@"entries"] = @[@{@"path":@"../escape", @"directory":@NO, @"data":[NSData data]}];
        CHECK(writePlist(bad, remoteURL, &error), "malformed cloud fixture");
        CHECK(![device(cloud, a) prepare:&error] && ![fm fileExistsAtPath:child(root, @"escape").path], "path traversal refused");
        CHECK([get(a, @"progress") isEqual:@"pending-c"], "invalid import preserves committed save");
        bad[@"entries"] = @[@{@"path":@"missing-parent/file", @"directory":@NO, @"data":[NSData data]}];
        CHECK(!restore(bad, a, &error) && [get(a, @"progress") isEqual:@"pending-c"], "failed staging preserves original save");
        // One container serves independent games and independent settings slots.
        NSURL *other = child(root, @"other-game"), *settings = child(root, @"settings"), *settings2 = child(root, @"settings-device2");
        for (NSURL *u in @[other, settings, settings2]) [fm createDirectoryAtURL:u withIntermediateDirectories:YES attributes:nil error:NULL];
        put(other, @"progress", @"other-game-progress");
        LocalCloud *otherStore = [[LocalCloud alloc] initWithFolder:cloud local:other title:@"0100F2C0115B6000"];
        CHECK([otherStore prepare:&error], "another game shares the container without reading the first game's save");
        put(settings, @"preferences.plist", @"synthetic preferences");
        [fm createDirectoryAtURL:child(settings, @"cheats") withIntermediateDirectories:YES attributes:nil error:NULL];
        put(child(settings, @"cheats"), @"toggles.txt", @"[Test cheat] on");
        LocalCloud *config = [[LocalCloud alloc] initWithFolder:cloud local:settings title:@"0100F2C0115B6000-configuration"];
        CHECK([config prepare:&error], "settings publish separately from game progress");
        LocalCloud *config2 = [[LocalCloud alloc] initWithFolder:cloud local:settings2 title:@"0100F2C0115B6000-configuration"];
        CHECK([config2 prepare:&error] && [get(child(settings2, @"cheats"), @"toggles.txt") isEqual:@"[Test cheat] on"], "cheat switches and configuration reach second device");
        CHECK([get(other, @"progress") isEqual:@"other-game-progress"], "configuration sync does not change gameplay progress");
        NSURL *gameFolder = child(cloud, @"directory-game"), *meta = child(gameFolder, @"nxnative-backup");
        [fm createDirectoryAtURL:meta withIntermediateDirectories:YES attributes:nil error:NULL];
        NSURL *rawLocal = child(root, @"raw-local"), *rawSecond = child(root, @"raw-second");
        for (NSURL *u in @[rawLocal, rawSecond]) [fm createDirectoryAtURL:u withIntermediateDirectories:YES attributes:nil error:NULL];
        put(rawLocal, @"progress", @"raw-progress");
        LocalCloud *raw = [[LocalCloud alloc] initWithFolder:meta local:rawLocal title:@"save"];
        raw.mirror = child(gameFolder, @"save");
        CHECK([raw prepare:&error], "migrate local saves into game/save directory");
        CHECK([get(raw.mirror, @"progress") isEqual:@"raw-progress"] && [get(rawLocal, @"progress") isEqual:@"raw-progress"], "cloud uses ordinary files and migration retains local save");
        LocalCloud *raw2 = [[LocalCloud alloc] initWithFolder:meta local:rawSecond title:@"save"];
        raw2.mirror = raw.mirror;
        CHECK([raw2 prepare:&error] && [get(rawSecond, @"progress") isEqual:@"raw-progress"], "explicit sync restores local save from raw cloud directory");
        put(rawLocal, @"progress", @"explicit-local-backup");
        CHECK([raw backupLocal:&error] && [get(rawLocal, @"progress") isEqual:@"explicit-local-backup"], "backup uploads local progress without importing cloud data");
        CHECK([get(raw.mirror, @"progress") isEqual:@"explicit-local-backup"], "explicit backup replaces the cloud copy");
        NSDictionary *download = [raw2 downloadSnapshot:&error];
        CHECK(download && [get(rawSecond, @"progress") isEqual:@"raw-progress"], "download preflight does not change local files or upload pending data");
        CHECK([raw backupLocal:&error], "repeating a completed backup is idempotent");
        raw.uploadPending = YES;
        put(rawLocal, @"progress", @"queued-before-upload");
        NSDictionary *queued = [raw enqueueBackup:&error];
        CHECK(queued && [get(raw.mirror, @"progress") isEqual:@"queued-before-upload"], "queued backup succeeds while server upload is pending");
        CHECK(![raw backupLocal:&error] && error.code == 2, "queued backup does not imply confirmed upload");
        CHECK([[[raw enqueueBackup:&error] objectForKey:@"revision"] isEqual:queued[@"revision"]], "repeated queue preserves the revision while upload is pending");
        for (NSString *slot in @[@"save_device", @"cheats", @"settings"]) {
            LocalCloud *nextSlot = [[LocalCloud alloc] initWithFolder:meta local:rawLocal title:slot];
            nextSlot.mirror = child(gameFolder, slot); nextSlot.uploadPending = YES;
            CHECK([nextSlot enqueueBackup:&error] != nil, "every remaining slot can be queued without server acknowledgement");
        }
        put(raw.mirror, @"progress", @"partial-or-external-edit");
        CHECK(![raw2 prepare:&error] && [get(rawSecond, @"progress") isEqual:@"raw-progress"], "incomplete cloud tree cannot replace local progress");
        CHECK(dm_cloud_prepare() == 1, "local boot never requires iCloud access");
        NSURL *restoreDocs = child(root, @"restore-documents");
        NSArray *slotNames = backupNames();
        CHECK(([slotNames isEqual:@[@"save", @"cheats"]]), "manual backup includes only save and cheats");
        NSURL *recoveryRoot = child(root, @"RestoreBackups");
        [fm createDirectoryAtURL:restoreDocs withIntermediateDirectories:YES attributes:nil error:NULL];
        for (NSString *file in @[@"log.txt", @"save.zip", @"pso_user.tsv", @"settings.plist"]) put(restoreDocs, file, @"leave-untouched");
        NSMutableArray *targets = [NSMutableArray array], *incoming = [NSMutableArray array];
        for (NSString *slot in slotNames) {
            NSURL *target = child(restoreDocs, slot), *source = child(child(root, @"incoming"), slot);
            [fm createDirectoryAtURL:target withIntermediateDirectories:YES attributes:nil error:NULL];
            [fm createDirectoryAtURL:source withIntermediateDirectories:YES attributes:nil error:NULL];
            put(target, @"data", @"before-restore"); put(source, @"data", @"restored-backup");
            if ([slot isEqual:@"settings"]) writePlist(@{}, child(source, @"preferences.plist"), NULL);
            [targets addObject:target]; [incoming addObject:@{@"entries":snapshot(source, NULL)}];
        }
        CHECK(installBackup(incoming, targets, slotNames, recoveryRoot, &error), "restore stages and installs every local directory");
        for (NSURL *target in targets) CHECK([get(target, @"data") isEqual:@"restored-backup"], "restore installs expected local bytes");
        NSArray *backups = [fm contentsOfDirectoryAtPath:recoveryRoot.path error:NULL];
        CHECK(backups.count == 1 && [get(child(child(recoveryRoot, backups.firstObject), @"save"), @"data") isEqual:@"before-restore"], "restore preserves previous local save in a recoverable backup");
        for (NSString *file in @[@"log.txt", @"save.zip", @"pso_user.tsv", @"settings.plist"]) CHECK([get(restoreDocs, file) isEqual:@"leave-untouched"], "restore leaves all other Documents files untouched");
        incoming[1] = @{@"entries":@[@{@"path":@"missing-parent/file", @"directory":@NO, @"data":[NSData data]}]};
        CHECK(!installBackup(incoming, targets, slotNames, recoveryRoot, &error) && [get(targets[0], @"data") isEqual:@"restored-backup"], "invalid cheats staging aborts the whole restore before changing any local save");
        CHECK([fm removeItemAtURL:root error:NULL], "remove fixture");
        printf("cloud save: %d failed\n", failures);
    }
    return failures ? 1 : 0;
}

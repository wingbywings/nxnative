// iCloud transports one complete binary-plist snapshot, never the guest's live
// working tree. Local Horizon commits remain atomic; a durable outbox survives
// suspension and upload failures. Revision checks refuse concurrent writers.
#import "cloud_save.h"
#import <Network/Network.h>
#import <TargetConditionals.h>
#if !TARGET_OS_IPHONE
#import <AppKit/AppKit.h>
#endif
#include <stdatomic.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <stdio.h>
#include "game_profile.h"

static NSString *const GuestName = @"org.d2rmobile.guest-name";
static BOOL fail(NSError **error, NSString *message) {
    if (error) *error = [NSError errorWithDomain:@"nxnative.iCloud" code:1 userInfo:@{NSLocalizedDescriptionKey:message}];
    return NO;
}
static BOOL waiting(NSError **error, NSString *message) {
    if (error) *error = [NSError errorWithDomain:@"nxnative.iCloud" code:2 userInfo:@{NSLocalizedDescriptionKey:message}];
    return NO;
}
static NSURL *child(NSURL *root, NSString *name) { return [root URLByAppendingPathComponent:name]; }
// A remote document may have no local filesystem entry yet. Run the metadata
// query on the main run loop while the explicit restore worker waits for it.
static BOOL discoverBackup(NSURL *marker, NSError **error) {
    if (NSThread.isMainThread) return fail(error, @"Backup discovery must run off the main thread.");
    dispatch_semaphore_t finished = dispatch_semaphore_create(0);
    __block NSMetadataQuery *query;
    __block id observer;
    __block BOOL gathered = NO, found = NO;
    dispatch_sync(dispatch_get_main_queue(), ^{
        query = [NSMetadataQuery new];
        query.searchScopes = @[NSMetadataQueryUbiquitousDocumentsScope];
        query.predicate = [NSPredicate predicateWithFormat:@"%K == %@", NSMetadataItemFSNameKey, marker.lastPathComponent];
        observer = [NSNotificationCenter.defaultCenter addObserverForName:NSMetadataQueryDidFinishGatheringNotification object:query queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *note) {
            (void)note; gathered = YES;
            for (NSMetadataItem *item in query.results) {
                NSURL *url = [item valueForAttribute:NSMetadataItemURLKey];
                if ([url.URLByStandardizingPath.path isEqual:marker.URLByStandardizingPath.path]) { found = YES; break; }
            }
            dispatch_semaphore_signal(finished);
        }];
        if (![query startQuery]) dispatch_semaphore_signal(finished);
    });
    dispatch_semaphore_wait(finished, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC));
    dispatch_sync(dispatch_get_main_queue(), ^{
        [query stopQuery]; [NSNotificationCenter.defaultCenter removeObserver:observer];
    });
    if (found) return YES;
    return fail(error, gathered ? @"No completed backup for this game is available in iCloud on this device. On the device with your saves, use Back up to iCloud first. Older backups need to be backed up again with this version. Local files were not changed." : @"iCloud backup discovery has not completed. Check iCloud Drive access and retry Restore. Local files were not changed.");
}
static BOOL writePlist(id object, NSURL *url, NSError **error) {
    NSData *data = [NSPropertyListSerialization dataWithPropertyList:object format:NSPropertyListBinaryFormat_v1_0 options:0 error:error];
    return data && [data writeToURL:url options:NSDataWritingAtomic error:error];
}
static NSFileCoordinator *coordinatorWithDeadline(void) {
    NSFileCoordinator *coordinator = [[NSFileCoordinator alloc] initWithFilePresenter:nil];
    __weak NSFileCoordinator *weak = coordinator;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{ [weak cancel]; });
    return coordinator;
}
static id readPlist(NSURL *url, NSError **error) {
    NSData *data = [NSData dataWithContentsOfURL:url options:0 error:error];
    return data ? [NSPropertyListSerialization propertyListWithData:data options:NSPropertyListImmutable format:NULL error:error] : nil;
}
// Preserve the guest-name xattr used for case-sensitive Horizon filenames.
// Reject links and special files instead of reading outside the save tree.
static NSArray *snapshot(NSURL *root, NSError **error) {
    root = root.URLByResolvingSymlinksInPath;
    NSFileManager *fm = NSFileManager.defaultManager;
    __block NSError *scanError;
    NSDirectoryEnumerator *scan = [fm enumeratorAtURL:root includingPropertiesForKeys:nil options:0 errorHandler:^BOOL(NSURL *u, NSError *e) { (void)u; scanError = e; return NO; }];
    if (!scan) { fail(error, @"Cannot enumerate the local save."); return nil; }
    NSMutableArray *entries = [NSMutableArray array];
    for (NSURL *u in scan) {
        struct stat st;
        if (lstat(u.fileSystemRepresentation, &st) || (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode))) {
            fail(error, @"The save contains an unreadable entry or a symbolic link."); return nil;
        }
        NSString *path = [u.URLByResolvingSymlinksInPath.path substringFromIndex:root.path.length + 1];
        NSMutableDictionary *entry = [@{@"path":path, @"directory":@(S_ISDIR(st.st_mode))} mutableCopy];
        if (S_ISREG(st.st_mode)) {
            NSData *bytes = [NSData dataWithContentsOfURL:u options:0 error:error];
            if (!bytes) return nil;
            entry[@"data"] = bytes;
        }
        char name[0x301]; ssize_t n = getxattr(u.fileSystemRepresentation, GuestName.UTF8String, name, sizeof name, 0, XATTR_NOFOLLOW);
        if (n >= 0) entry[@"guestName"] = [NSData dataWithBytes:name length:(NSUInteger)n];
        else if (errno != ENOATTR) { fail(error, @"Cannot read a save filename attribute."); return nil; }
        [entries addObject:entry];
    }
    if (scanError) { if (error) *error = scanError; return nil; }
    [entries sortUsingComparator:^NSComparisonResult(id a, id b) { return [a[@"path"] compare:b[@"path"] options:NSLiteralSearch]; }];
    return entries;
}
static BOOL validSnapshot(id s, NSString *title, NSError **error) {
    if (![s isKindOfClass:NSDictionary.class] || ![s[@"format"] isEqual:@1] || ![s[@"title"] isEqual:title] ||
        ![s[@"revision"] isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:s[@"revision"]] || ![s[@"entries"] isKindOfClass:NSArray.class])
        return fail(error, @"The cloud save is invalid or belongs to a different game.");
    if (s[@"ancestors"]) {
        if (![s[@"ancestors"] isKindOfClass:NSArray.class]) return fail(error, @"Invalid save history.");
        for (id revision in s[@"ancestors"])
            if (![revision isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:revision]) return fail(error, @"Invalid save history.");
    }
    NSMutableSet *paths = [NSMutableSet set];
    for (id e in s[@"entries"]) {
        if (![e isKindOfClass:NSDictionary.class]) return fail(error, @"Invalid save entry.");
        NSString *p = e[@"path"];
        if (![p isKindOfClass:NSString.class] || !p.length || [p hasPrefix:@"/"] || [p rangeOfCharacterFromSet:[NSCharacterSet characterSetWithRange:NSMakeRange(0, 1)]].location != NSNotFound)
            return fail(error, @"Unsafe save path.");
        for (NSString *part in [p componentsSeparatedByString:@"/"])
            if (!part.length || [part isEqual:@"."] || [part isEqual:@".."]) return fail(error, @"Unsafe save path.");
        if ([paths containsObject:p] || ![e[@"directory"] isKindOfClass:NSNumber.class] ||
            (![e[@"directory"] boolValue] && ![e[@"data"] isKindOfClass:NSData.class]) ||
            (e[@"guestName"] && (![e[@"guestName"] isKindOfClass:NSData.class] || [e[@"guestName"] length] > 0x300)))
            return fail(error, @"Invalid or duplicate save entry.");
        [paths addObject:p];
    }
    return YES;
}
static BOOL restore(NSDictionary *s, NSURL *local, NSError **error) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSURL *staging = [NSURL fileURLWithPath:[local.path stringByAppendingFormat:@".cloud-%@", NSUUID.UUID.UUIDString]];
    if (![fm createDirectoryAtURL:staging withIntermediateDirectories:NO attributes:nil error:error]) return NO;
    BOOL ok = YES;
    // Directories first; reject conflicting physical names on the destination FS.
    NSArray *entries = [s[@"entries"] sortedArrayUsingComparator:^NSComparisonResult(id a, id b) {
        return [a[@"path"] compare:b[@"path"] options:NSLiteralSearch];
    }];
    for (NSDictionary *e in entries) {
        NSURL *u = child(staging, e[@"path"]);
        if ([fm fileExistsAtPath:u.path]) { ok = fail(error, @"Conflicting save filenames."); break; }
        if ([e[@"directory"] boolValue]) ok = [fm createDirectoryAtURL:u withIntermediateDirectories:NO attributes:nil error:error];
        else ok = [e[@"data"] writeToURL:u options:NSDataWritingWithoutOverwriting error:error];
        if (!ok) break;
        NSData *name = e[@"guestName"];
        if (name && setxattr(u.fileSystemRepresentation, GuestName.UTF8String, name.bytes, name.length, 0, XATTR_NOFOLLOW)) {
            ok = fail(error, @"Cannot restore a save filename attribute."); break;
        }
    }
    if (ok && renamex_np(staging.fileSystemRepresentation, local.fileSystemRepresentation, RENAME_SWAP))
        ok = fail(error, @"Cannot atomically install the downloaded save. The local save was preserved.");
    [fm removeItemAtURL:staging error:NULL];
    return ok;
}

@implementation DMCloudSaveStore {
    NSURL *_folder, *_local, *_remote, *_journal;
    NSString *_title;
    NSMutableDictionary *_state;
    NSLock *_lock;
    NSError *_captureError;
    BOOL _deferUpload;
}
@synthesize mirror = _mirror;
- (void)setMirror:(NSURL *)mirror {
    _mirror = mirror;
    // Keep legacy snapshot journals untouched when opting into directory sync.
    _journal = [NSURL fileURLWithPath:[_local.path stringByAppendingString:@".directory-icloud-state.plist"]];
}
- (instancetype)initWithFolder:(NSURL *)folder local:(NSURL *)local title:(NSString *)title {
    if ((self = [super init])) {
        _folder = folder; _local = local; _title = title; _lock = [NSLock new];
        _remote = child(folder, [title stringByAppendingString:@".nxsave"]);
        _journal = [NSURL fileURLWithPath:[local.path stringByAppendingString:@".icloud-state.plist"]];
    }
    return self;
}
- (BOOL)checkCloud:(NSURL *)url uploaded:(BOOL)uploaded error:(NSError **)error {
    NSNumber *ubiquitous, *conflict, *sent; NSString *status; NSError *transfer;
    // Do not reuse NSURL's resource-value cache across checks.
    url = [NSURL fileURLWithPath:url.path];
    if (![url getResourceValue:&ubiquitous forKey:NSURLIsUbiquitousItemKey error:error]) return NO;
    if (!ubiquitous.boolValue) {
        if ([url.path isEqual:_folder.path]) return waiting(error, @"Waiting for the save folder to become available in iCloud Drive. Check iCloud access if this continues.");
        if (!uploaded) return fail(error, [NSString stringWithFormat:@"iCloud has not made %@ available on this device. Complete a backup on the source device, then retry Restore after iCloud finishes syncing. Local files were not changed.", url.lastPathComponent]);
        return waiting(error, [NSString stringWithFormat:@"iCloud has not registered %@ for upload yet. Retry Back up to iCloud to check completion.", url.lastPathComponent]);
    }
    if ([url.path isEqual:_folder.path]) return YES;
    if (![url getResourceValue:&conflict forKey:NSURLUbiquitousItemHasUnresolvedConflictsKey error:error] ||
        ![url getResourceValue:&status forKey:NSURLUbiquitousItemDownloadingStatusKey error:error] ||
        ![url getResourceValue:&transfer forKey:NSURLUbiquitousItemDownloadingErrorKey error:error]) return NO;
    if (conflict.boolValue || [NSFileVersion unresolvedConflictVersionsOfItemAtURL:url].count)
        return fail(error, @"iCloud has conflicting saves. Resolve the conflict in Files before playing; no version was overwritten.");
    if (transfer) { if (error) *error = transfer; return NO; }
    if (![status isEqual:NSURLUbiquitousItemDownloadingStatusCurrent]) {
        [NSFileManager.defaultManager startDownloadingUbiquitousItemAtURL:url error:NULL];
        return waiting(error, @"Waiting for the latest iCloud save to download. Keep the app open and retry.");
    }
    if (uploaded) {
        if (![url getResourceValue:&sent forKey:NSURLUbiquitousItemIsUploadedKey error:error] ||
            ![url getResourceValue:&transfer forKey:NSURLUbiquitousItemUploadingErrorKey error:error]) return NO;
        if (transfer) { if (error) *error = transfer; return NO; }
        if (!sent.boolValue) return waiting(error, @"Waiting for the save to upload to iCloud. Keep this app open before switching devices.");
        if (self.mirror) {
            NSDirectoryEnumerator *scan = [NSFileManager.defaultManager enumeratorAtURL:self.mirror includingPropertiesForKeys:@[NSURLIsDirectoryKey] options:0 errorHandler:nil];
            for (NSURL *item in scan) {
                NSNumber *directory;
                if (![item getResourceValue:&directory forKey:NSURLIsDirectoryKey error:error]) return NO;
                if (directory.boolValue) continue;
                if (![self checkCloud:item uploaded:NO error:error]) return NO;
                NSNumber *uploadedItem; NSString *downloadStatus;
                if (![item getResourceValue:&uploadedItem forKey:NSURLUbiquitousItemIsUploadedKey error:error] ||
                    ![item getResourceValue:&downloadStatus forKey:NSURLUbiquitousItemDownloadingStatusKey error:error]) return NO;
                if (!uploadedItem.boolValue || ![downloadStatus isEqual:NSURLUbiquitousItemDownloadingStatusCurrent])
                    return waiting(error, @"Cloud save files are still transferring. Local play is available.");
            }
        }
    }
    return YES;
}
- (BOOL)saveState:(NSMutableDictionary *)state error:(NSError **)error {
    if (!writePlist(state, _journal, error)) return NO;
    _state = state; return YES;
}
- (NSDictionary *)readRemote:(NSURL *)url error:(NSError **)error {
    if (![self checkCloud:url uploaded:NO error:error]) return nil;
    NSDictionary *s = readPlist(url, error);
    if (!validSnapshot(s, _title, error)) return nil;
    if (self.mirror) {
        for (NSDictionary *entry in s[@"entries"]) {
            if (![entry[@"directory"] boolValue] && ![self checkCloud:child(self.mirror, entry[@"path"]) uploaded:NO error:error]) return nil;
        }
        NSArray *entries = snapshot(self.mirror, error);
        if (!entries || (![entries isEqual:s[@"entries"]] && !([entries isEqual:_state[@"inFlight"][@"entries"]] && [s[@"revision"] isEqual:_state[@"base"][@"revision"]]))) {
            fail(error, @"Cloud files are still syncing or were edited separately. Local progress was preserved."); return nil;
        }
    }
    return s;
}
- (NSDictionary *)downloadSnapshot:(NSError **)error {
    __block NSDictionary *result; __block NSError *operationError; NSError *coordError;
    NSFileCoordinator *coordinator = coordinatorWithDeadline();
    [coordinator coordinateReadingItemAtURL:(self.mirror ? self.mirror.URLByDeletingLastPathComponent : _folder) options:0 error:&coordError byAccessor:^(NSURL *root) {
        NSURL *url = self.mirror ? child(child(root, @"nxnative-backup"), self->_remote.lastPathComponent) : child(root, self->_remote.lastPathComponent);
        result = [self readRemote:url error:&operationError];
    }];
    if (!result && error) *error = coordError ?: operationError;
    return result;
}
- (BOOL)backupLocal:(NSError **)error {
    NSArray *entries = snapshot(_local, error); if (!entries) return NO;
    NSDictionary *base;
    if ([NSFileManager.defaultManager fileExistsAtPath:_remote.path]) {
        base = [self downloadSnapshot:error]; if (!base) return NO;
        if ([base[@"entries"] isEqual:entries]) {
            _state = [@{@"base":base} mutableCopy];
            return _deferUpload || [self checkCloud:_remote uploaded:YES error:error];
        }
    } else if (self.mirror && [NSFileManager.defaultManager fileExistsAtPath:self.mirror.path]) {
        return fail(error, @"Cloud files have no backup metadata. Preserve them before replacing this backup.");
    }
    NSDictionary *pending = @{@"format":@1, @"title":_title, @"revision":NSUUID.UUID.UUIDString, @"entries":entries};
    NSMutableDictionary *next = [@{@"folder":_folder.path, @"base":base ?: pending, @"pending":pending} mutableCopy];
    if (!base) next[@"create"] = @YES;
    // Explicit local-to-cloud direction: never import a remote save first.
    if (![self saveState:next error:error]) return NO;
    return [self publish:error];
}
- (NSDictionary *)enqueueBackup:(NSError **)error {
    _deferUpload = YES;
    BOOL ok = [self backupLocal:error];
    _deferUpload = NO;
    return ok ? _state[@"base"] : nil;
}
- (BOOL)prepare:(NSError **)error {
    NSFileManager *fm = NSFileManager.defaultManager;
    if (![self checkCloud:_folder uploaded:NO error:error]) return NO;
    if (![fm createDirectoryAtURL:_local withIntermediateDirectories:YES attributes:nil error:error]) return NO;
    NSArray *localEntries = snapshot(_local, error); if (!localEntries) return NO;
    BOOL hasState = [fm fileExistsAtPath:_journal.path];
    if (hasState) {
        id stored = readPlist(_journal, error);
        if (![stored isKindOfClass:NSDictionary.class] || ![stored[@"folder"] isEqual:_folder.path] ||
            !validSnapshot(stored[@"base"], _title, error) ||
            (stored[@"pending"] && !validSnapshot(stored[@"pending"], _title, error)) ||
            (stored[@"inFlight"] && (!stored[@"pending"] || !validSnapshot(stored[@"inFlight"], _title, error))))
            return fail(error, @"The local sync journal is invalid or uses another folder. Local progress was preserved.");
        _state = [stored mutableCopy];
        NSDictionary *expected = _state[@"pending"] ?: _state[@"base"];
        if (![localEntries isEqual:expected[@"entries"]])
            return fail(error, @"Local progress changed outside cloud sync (or saving was interrupted). Back up and reconcile the local save before playing.");
        while (_state[@"pending"]) if (![self publish:error]) return NO;
    }
    __block NSDictionary *remote; __block NSError *operationError;
    NSFileCoordinator *coordinator = coordinatorWithDeadline(); NSError *coordError;
    [coordinator coordinateReadingItemAtURL:(self.mirror ? self.mirror.URLByDeletingLastPathComponent : _folder) options:0 error:&coordError byAccessor:^(NSURL *folder) {
        NSURL *u = child(self.mirror ? child(folder, @"nxnative-backup") : folder, self->_remote.lastPathComponent);
        NSError *existsError;
        if (![u checkResourceIsReachableAndReturnError:&existsError]) {
            if (existsError.code != NSFileReadNoSuchFileError || ![existsError.domain isEqual:NSCocoaErrorDomain]) operationError = existsError;
            return;
        }
        remote = [self readRemote:u error:&operationError];
        if (remote && ![self checkCloud:u uploaded:YES error:&operationError]) remote = nil;
    }];
    if (coordError || operationError) { if (error) *error = coordError ?: operationError; return NO; }
    if (!remote) {
        if (hasState) return fail(error, @"The previously synchronized cloud save is missing. Restore it in iCloud before playing.");
        if (self.mirror && [fm fileExistsAtPath:self.mirror.path])
            return fail(error, @"This cloud folder already contains files without sync metadata. Back them up before migrating; nothing was overwritten.");
        // The create flag permits only a missing document, never an overwrite.
        NSDictionary *base = @{@"format":@1, @"title":_title, @"revision":NSUUID.UUID.UUIDString, @"entries":localEntries};
        _state = [@{@"folder":_folder.path, @"base":base, @"create":@YES, @"pending":base} mutableCopy];
        if (![self saveState:_state error:error] || ![self publish:error]) return NO;
        return [self checkCloud:_remote uploaded:YES error:error];
    }
    if (hasState && ![remote[@"revision"] isEqual:_state[@"base"][@"revision"]] &&
        ![remote[@"ancestors"] containsObject:_state[@"base"][@"revision"]])
        return fail(error, @"The cloud save diverged from this device's last version. Local and cloud progress were preserved; reconcile them before playing.");
    if (![localEntries isEqual:remote[@"entries"]]) {
        // A first-time cloud import preserves existing device progress separately.
        if (!hasState && localEntries.count) {
            NSURL *backup = [NSURL fileURLWithPath:[_local.path stringByAppendingFormat:@".before-icloud-%@", NSUUID.UUID.UUIDString]];
            if (![fm copyItemAtURL:_local toURL:backup error:error]) return NO;
        }
        if (!restore(remote, _local, error)) return NO;
    }
    return [self saveState:[@{@"folder":_folder.path, @"base":remote} mutableCopy] error:error];
}
- (BOOL)capture:(NSError **)error {
    NSError *captureError;
    NSArray *entries = snapshot(_local, &captureError);
    if (!entries) {
        [_lock lock]; _captureError = captureError; [_lock unlock];
        if (error) *error = captureError; return NO;
    }
    [_lock lock];
    NSMutableDictionary *next = [_state mutableCopy];
    next[@"pending"] = @{@"format":@1, @"title":_title, @"revision":NSUUID.UUID.UUIDString, @"entries":entries};
    BOOL ok = [self saveState:next error:&captureError];
    _captureError = ok ? nil : captureError;
    if (!ok && error) *error = captureError;
    [_lock unlock]; return ok;
}
- (BOOL)publish:(NSError **)error {
    [_lock lock];
    if (_captureError) { if (error) *error = _captureError; [_lock unlock]; return NO; }
    NSDictionary *state = [_state copy];
    NSMutableDictionary *pending = [(state[@"inFlight"] ?: state[@"pending"]) mutableCopy];
    if (!pending) { [_lock unlock]; return [self checkCloud:_remote uploaded:YES error:error]; }
    NSMutableArray *ancestors = [state[@"base"][@"ancestors"] mutableCopy] ?: [NSMutableArray array];
    if (![state[@"create"] boolValue]) [ancestors addObject:state[@"base"][@"revision"]];
    pending[@"ancestors"] = ancestors;
    // Persist which revision is being published before releasing the lock. A
    // newer local commit may arrive during coordination; after a crash we must
    // recognize our earlier upload before publishing that newer pending save.
    NSMutableDictionary *sending = [_state mutableCopy]; sending[@"inFlight"] = pending;
    if (![self saveState:sending error:error]) { [_lock unlock]; return NO; }
    [_lock unlock];
    __block NSError *operationError; __block BOOL ok = NO; NSError *coordError;
    NSFileCoordinator *coordinator = coordinatorWithDeadline();
    [coordinator coordinateWritingItemAtURL:(self.mirror ? self.mirror.URLByDeletingLastPathComponent : _remote) options:0 error:&coordError byAccessor:^(NSURL *coordinated) {
        NSURL *u = self.mirror ? child(child(coordinated, @"nxnative-backup"), self->_remote.lastPathComponent) : coordinated;
        NSError *existsError;
        BOOL exists = [u checkResourceIsReachableAndReturnError:&existsError];
        if (!exists && (existsError.code != NSFileReadNoSuchFileError || ![existsError.domain isEqual:NSCocoaErrorDomain])) { operationError = existsError; return; }
        NSDictionary *remote = exists ? [self readRemote:u error:&operationError] : nil;
        if (exists && !remote) return;
        if ([remote[@"revision"] isEqual:pending[@"revision"]]) { ok = YES; return; } // crash after publish, before journal update
        if ((exists && ([state[@"create"] boolValue] || ![remote[@"revision"] isEqual:state[@"base"][@"revision"]])) ||
            (!exists && ![state[@"create"] boolValue])) {
            fail(&operationError, @"Another device changed the cloud save. This device's progress is retained locally; cloud progress was not overwritten."); return;
        }
        if (self.mirror) {
            NSFileManager *fm = NSFileManager.defaultManager;
            if (![fm createDirectoryAtURL:self.mirror withIntermediateDirectories:YES attributes:nil error:&operationError] || !restore(pending, self.mirror, &operationError)) return;
        }
        ok = writePlist(pending, u, &operationError);
    }];
    if (!ok) { if (error) *error = coordError ?: operationError; return NO; }
    [_lock lock];
    NSMutableDictionary *next = [_state mutableCopy]; next[@"base"] = pending; [next removeObjectForKey:@"create"];
    [next removeObjectForKey:@"inFlight"];
    if ([next[@"pending"][@"revision"] isEqual:pending[@"revision"]]) [next removeObjectForKey:@"pending"];
    ok = [self saveState:next error:error];
    // Do not report fully synced if another commit arrived during this upload.
    BOOL more = next[@"pending"] != nil;
    [_lock unlock];
    if (ok && more) return waiting(error, @"New progress is queued for iCloud. Keep this app open.");
    return ok && (_deferUpload || [self checkCloud:_remote uploaded:YES error:error]);
}
@end

static void (^statusHandler)(NSString *, BOOL);
void dm_cloud_status_handler(void (^handler)(NSString *, BOOL)) { statusHandler = [handler copy]; }
// Only save and cheats belong to this manual backup. Recovery copies live
// outside Documents, so a later backup cannot recursively include them.
static BOOL installBackup(NSArray *snapshots, NSArray *locals, NSArray *names, NSURL *recoveryRoot, NSError **error) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSMutableArray<NSURL *> *stages = [NSMutableArray array];
    NSMutableArray<NSURL *> *created = [NSMutableArray array];
    NSString *transaction = NSUUID.UUID.UUIDString;
    BOOL ok = YES;
    for (NSUInteger i = 0; i < locals.count; i++) {
        NSURL *stage = [NSURL fileURLWithPath:[((NSURL *)locals[i]).path stringByAppendingFormat:@".restore-%@", transaction]];
        [stages addObject:stage];
        if (![fm createDirectoryAtURL:stage withIntermediateDirectories:YES attributes:nil error:error] || !restore(snapshots[i], stage, error)) { ok = NO; break; }
    }
    NSURL *backup = child(recoveryRoot, transaction);
    if (ok) ok = [fm createDirectoryAtURL:backup withIntermediateDirectories:YES attributes:nil error:error];
    for (NSUInteger i = 0; ok && i < locals.count; i++) {
        NSURL *url = locals[i];
        if ([fm fileExistsAtPath:url.path]) ok = [fm copyItemAtURL:url toURL:child(backup, names[i]) error:error];
        else { ok = [fm createDirectoryAtURL:url withIntermediateDirectories:YES attributes:nil error:error]; if (ok) [created addObject:url]; }
    }
    NSUInteger swapped = 0;
    if (ok) for (; swapped < locals.count; swapped++) {
        if (renamex_np(stages[swapped].fileSystemRepresentation, ((NSURL *)locals[swapped]).fileSystemRepresentation, RENAME_SWAP)) {
            ok = fail(error, @"Cannot install the backup. Previous files are retained in Library/Application Support/RestoreBackups."); break;
        }
    }
    BOOL rolledBack = YES;
    if (!ok) while (swapped) {
        swapped--;
        if (renamex_np(stages[swapped].fileSystemRepresentation, ((NSURL *)locals[swapped]).fileSystemRepresentation, RENAME_SWAP)) rolledBack = NO;
    }
    if (!rolledBack) fail(error, @"Restore rollback failed. Recover previous files from Library/Application Support/RestoreBackups before playing.");
    if (!ok && rolledBack) for (NSURL *url in created) [fm removeItemAtURL:url error:NULL];
    for (NSURL *stage in stages) [fm removeItemAtURL:stage error:NULL];
    return ok;
}
static NSArray<NSString *> *backupNames(void) { return @[@"save", @"cheats"]; }
static BOOL transfer(BOOL downloading, NSError **error) {
    NSString *identifier = [NSBundle.mainBundle objectForInfoDictionaryKey:@"DMICloudContainerIdentifier"];
    if (![identifier isKindOfClass:NSString.class] || !identifier.length) return fail(error, @"No iCloud container is configured. Local play is available.");
    NSFileManager *fm = NSFileManager.defaultManager;
    NSURL *container = [fm URLForUbiquityContainerIdentifier:identifier];
    if (!container) return fail(error, @"iCloud is unavailable. You can continue playing locally.");
    NSURL *documents = [fm URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
    NSArray *names = backupNames();
    NSArray *locals = @[child(documents, @"save"), child(documents, @"cheats")];
    NSURL *game = child(child(container, @"Documents"), @DM_GAME_ID), *metadata = child(game, @"nxnative-backup");
    if (!downloading && ![fm createDirectoryAtURL:metadata withIntermediateDirectories:YES attributes:nil error:error]) return NO;
    NSMutableArray *snapshots = [NSMutableArray array];
    NSURL *marker = child(metadata, @"backup.plist");
    NSDictionary *generation;
    if (downloading) {
        if (!discoverBackup(marker, error)) return NO;
        DMCloudSaveStore *probe = [[DMCloudSaveStore alloc] initWithFolder:metadata local:locals[0] title:@"save"];
        if (![probe checkCloud:marker uploaded:NO error:error]) return NO;
        generation = readPlist(marker, error);
        if (![generation isKindOfClass:NSDictionary.class]) return fail(error, @"No completed backup is available for this game.");
    }
    for (NSUInteger i = 0; i < names.count; i++) {
        DMCloudSaveStore *store = [[DMCloudSaveStore alloc] initWithFolder:metadata local:locals[i] title:names[i]];
        store.mirror = child(game, names[i]);
        if (downloading) {
            NSDictionary *value = [store downloadSnapshot:error]; if (!value) return NO;
            if (![generation[names[i]] isEqual:value[@"revision"]]) return fail(error, @"The cloud backup is incomplete or is being replaced. Local files were not changed.");
            [snapshots addObject:value];
        } else {
            if (![fm createDirectoryAtURL:locals[i] withIntermediateDirectories:YES attributes:nil error:error]) return NO;
            NSDictionary *value = [store enqueueBackup:error]; if (!value) return NO;
            [snapshots addObject:value];
        }
    }
    if (!downloading) {
        __block NSError *operationError; NSError *coordError; __block BOOL complete = NO;
        NSFileCoordinator *coordinator = coordinatorWithDeadline();
        [coordinator coordinateWritingItemAtURL:game options:0 error:&coordError byAccessor:^(NSURL *root) {
            NSMutableDictionary *revisions = [NSMutableDictionary dictionary];
            for (NSUInteger i = 0; i < names.count; i++) {
                NSDictionary *current = readPlist(child(child(root, @"nxnative-backup"), [names[i] stringByAppendingString:@".nxsave"]), &operationError);
                if (![current[@"revision"] isEqual:snapshots[i][@"revision"]]) { fail(&operationError, @"Another backup changed during upload. Retry to complete this backup."); return; }
                revisions[names[i]] = current[@"revision"];
            }
            NSURL *record = child(child(root, @"nxnative-backup"), @"backup.plist");
            complete = [readPlist(record, NULL) isEqual:revisions] || writePlist(revisions, record, &operationError);
        }];
        if (!complete) { if (error) *error = coordError ?: operationError; return NO; }
        // Submission is complete; server upload is asynchronous. Restore checks
        // both directory revisions before changing local data.
        return YES;
    }
    NSURL *support = [fm URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask].firstObject;
    return installBackup(snapshots, locals, names, child(support, @"RestoreBackups"), error);
}

BOOL dm_cloud_backup_container(NSError **error) { return transfer(NO, error); }
BOOL dm_cloud_restore_container(NSError **error) { return transfer(YES, error); }
int dm_cloud_prepare(void) { return 1; }
void dm_cloud_configuration_changed(void) {} // Explicit backup captures current settings.
void dm_cloud_committed(const char *path) { (void)path; } // Playing never changes the cloud backup implicitly.

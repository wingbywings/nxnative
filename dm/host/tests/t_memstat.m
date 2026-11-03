// Set diagnostics after constructors, as the iOS env-file loader does.
#import <Foundation/Foundation.h>
#include <pthread.h>
#include <time.h>
static int creates, fail_create;
static int counted_create(pthread_t *t, const pthread_attr_t *a, void *(*fn)(void *), void *p) {
    creates++;
    return fail_create ? EAGAIN : pthread_create(t, a, fn, p);
}
#define pthread_create counted_create
#include "../memstat.m"
#undef pthread_create

module_t *g_mod;
int g_nmod;
uint64_t now_ns(void) { return clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW); }
void svc_guest_range(uint8_t **base, size_t *size) { *base = NULL; *size = 0; }
int thr_name_of(pthread_t t, char *name, size_t size) { (void)t; (void)name; (void)size; return 0; }
static int checks, failures;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failures++; } } while (0)
static NSString *wait_for(NSString *path, NSString *needle) {
    for (int i = 0; i < 500; i++) {
        NSString *s = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
        if ([s containsString:needle]) return s;
        usleep(10000);
    }
    return @"";
}
int main(int argc, char **argv) { @autoreleasepool {
    (void)argv;
    alarm(15);
    CHECK(creates == 0);   // No launch environment: constructors must not consume the once tokens.
    dm_diagnostics_start();
    CHECK(creates == 0);
    char dir[] = "/tmp/nx-memstat-XXXXXX";
    CHECK(mkdtemp(dir) != NULL);
    NSString *root = @(dir), *memory = [root stringByAppendingPathComponent:@"memory.tsv"];
    NSString *cpu = [root stringByAppendingPathComponent:@"cpu.tsv"];
    setenv("DM_MEMSTAT", memory.fileSystemRepresentation, 1);
    fail_create = argc > 1;
    if (fail_create) {
        dm_diagnostics_start();
        CHECK(creates == 1);
        CHECK(![[NSFileManager defaultManager] fileExistsAtPath:memory]);
        dm_diagnostics_start();
        CHECK(creates == 1);   // A failed startup is reported, not retried on each call.
    } else {
        setenv("DM_THRCPU", [cpu stringByAppendingString:@",1"].UTF8String, 1);
        dm_diagnostics_start();
        NSString *data = wait_for(memory, @"\n0\t");
        CHECK([data containsString:@"phys_footprint"]);
        CHECK([data containsString:@"footprint_peak"]);
        CHECK([data containsString:@"\n0\t"]);   // A real VM sample, not just file creation.
        CHECK([wait_for(cpu, @"\t-\ttotal\t") containsString:@"thread ID"]);
        CHECK(creates == 2);
        dm_diagnostics_start(); dm_diagnostics_start();
        CHECK(creates == 2);   // No extra writers or truncation on repeated initialization.
        CHECK(memstat_thread(strdup("/dev/null/memory.tsv")) == NULL);
        CHECK(thrcpu_thread(strdup("/dev/null/cpu.tsv")) == NULL);
    }
    [[NSFileManager defaultManager] removeItemAtPath:root error:NULL];
    fprintf(stderr, "memstat: %d checks, %d failures\n", checks, failures);
    return failures != 0;
} }

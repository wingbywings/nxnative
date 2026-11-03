// Real Metal allocator high-water retention, pressure policy and GPU lifetime.
#include "../nvn/nvn_mtl.m"
void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return bp_now(); }
static int checks, fails;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)
@interface NoAllocator : NSObject
@end
@implementation NoAllocator
- (id)newCommandAllocatorWithDescriptor:(id)d error:(NSError **)e { (void)d; (void)e; return nil; }
@end
static void fill_many(id<MTL4CommandBuffer> cb, id<MTLBuffer> b) {
    id<MTL4ComputeCommandEncoder> e = [cb computeCommandEncoder];
    for (int i = 0; i < 200000; i++) [e fillBuffer:b range:NSMakeRange((NSUInteger)i * 4, 4) value:63];
    [e endEncoding];
}
static void grow(id<MTL4CommandAllocator> a, id<MTLBuffer> b) { @autoreleasepool {
    id<MTL4CommandBuffer> cb = [g_dev newCommandBuffer];
    [cb beginCommandBufferWithAllocator:a]; fill_many(cb, b); [cb endCommandBuffer];
    // Deliberately never submitted: no GPU work can still reference this allocator.
} }
int main(void) { @autoreleasepool {
    alarm(90); setbuf(stdout, NULL); setenv("DM_NVN_ALLOC_TRIM", "1", 1);
    mtl_init();
    id<MTLBuffer> b = [g_dev newBufferWithLength:1u << 20 options:MTLResourceStorageModeShared]; res_add(b); res_commit();
    NvAllocMemory s = {0};
    id<MTL4CommandAllocator> a = [g_dev newCommandAllocator];
    grow(a, b); uint64_t big = a.allocatedSize;
    CHECK(big > ALLOC_TRIM_BYTES, "burst exceeds the allocator retirement threshold");
    __weak id original = a;
    a = alloc_recycle(a, &s, NSEC_PER_SEC, UINT64_MAX, 1);
    CHECK(a == original && a.allocatedSize >= big, "normal reset reuses the burst allocation");
    a = alloc_recycle(a, &s, 2ull * NSEC_PER_SEC, 0, 0);
    CHECK(a == original, "A/B switch keeps the allocator under pressure");
    @autoreleasepool { a = alloc_recycle(a, &s, 3ull * NSEC_PER_SEC, 0, 1); }
    CHECK(original == nil, "retirement releases the old allocator");
    CHECK(a.allocatedSize < ALLOC_TRIM_BYTES, "replacement starts with small capacity");
    CHECK(s.replacements == 1 && s.retired_bytes == big && s.peak >= big, "capacity counters record the real retired allocator");
    grow(a, b); original = a;
    a = alloc_recycle(a, &s, 4ull * NSEC_PER_SEC, 0, 1);
    CHECK(a == original, "cooldown prevents per-frame recreation");
    id<MTLDevice> device = g_dev; g_dev = (id)[NoAllocator new];
    a = alloc_recycle(a, &s, 14ull * NSEC_PER_SEC, 0, 1);
    g_dev = device;
    CHECK(a == original && s.failures == 1, "allocation failure preserves the usable old allocator");
    @autoreleasepool { a = alloc_recycle(a, &s, 25ull * NSEC_PER_SEC, 0, 1); }
    CHECK(original == nil && s.replacements == 2, "a later successful retirement releases the old allocation");
    original = a; a = alloc_recycle(a, &s, 40ull * NSEC_PER_SEC, 0, 1);
    CHECK(a == original && s.replacements == 2, "small allocators stay reusable even under pressure");

    // Real production selection: a busy allocator is skipped, not reset/retired.
    id<MTLSharedEvent> gate = [g_dev newSharedEvent];
    [g_q4 waitForEvent:gate value:1];
    mtl_sub_begin(); int slot = g_alloc_cur; original = g_alloc4[slot];
    fill_many(g_cb, b); uint64_t first = mtl_sub_commit();
    g_alloc_mem[slot].checked_at = 0;
    mtl_sub_begin();
    CHECK(g_alloc_cur != slot && original != nil, "in-flight allocator remains alive and is not reused");
    uint64_t second = mtl_sub_commit(); gate.signaledValue = 1;
    CHECK(mtl_wait(second, 10000000000ull) && g_done >= first, "GPU completes both submissions");
    CHECK(!mtl_last_commit_error(), "GPU reports no submission error");
    CHECK(((uint8_t *)b.contents)[0] == 63 && ((uint8_t *)b.contents)[199999 * 4] == 63, "submitted commands survive retirement policy");
    mtl_sub_begin();
    CHECK(g_alloc_cur == slot && g_alloc_mem[slot].replacements == 1, "completed oversized allocator is replaced at its next use");
    CHECK(original == nil, "no stale encoding reference pins the retired main allocator");
    CHECK(mtl_wait(mtl_sub_commit(), 10000000000ull), "submission through replacement completes");

    // Upload slots use their own completion condition rather than the main fence.
    unsigned upload_slot = g_up_seq % 3;
    up_wait_zero(&g_up_alloc_busy[upload_slot], WS_DIAG);
    grow(g_up_alloc4[upload_slot], b); original = g_up_alloc4[upload_slot];
    g_up_alloc_mem[upload_slot].checked_at = 0;
    up_begin();
    CHECK(g_up_alloc_mem[upload_slot].replacements == 1, "completed oversized upload allocator is replaced");
    CHECK(original == nil, "upload slot releases its old allocator");
    [g_up_ce fillBuffer:b range:NSMakeRange(0, 4096) value:91];
    up_flush(); rec_publish(); up_wait_zero(&g_up_pending, WS_DIAG); rec_drain();
    CHECK(((uint8_t *)b.contents)[0] == 91, "upload through replacement completes with correct contents");
    alloc_mem_stat();
    printf("allocator memory: burst %llu MiB, %d checks, %d failed\n", (unsigned long long)(big >> 20), checks, fails);
    return fails != 0;
} }

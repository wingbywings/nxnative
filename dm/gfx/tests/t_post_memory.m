// Real GPU readbacks completed by a fast guest waiter must release their buffers
// while the guest thread's outer autorelease pool is still open.
#include "../nvn/nvn_mtl.m"
void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return bp_now(); }
static int checks, fails;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)
static size_t alive_bytes(NSHashTable *buffers) { @autoreleasepool {
    size_t bytes = 0;
    for (id<MTLBuffer> b in buffers) bytes += b.length;
    return bytes;
} }
int main(void) { @autoreleasepool {
    alarm(60); mtl_init();
    const size_t bytes = 512u * 512u * 4u;
    uint8_t *mem = calloc(1, bytes);
    pool_t pool = {.mem=mem, .size=bytes};
    tex_t guest = {.serial=1, .mem=mem, .pool=&pool,
        .d={.target=NVN_TEXTURE_TARGET_2D, .format=NVN_FORMAT_RGBA8,
            .width=512, .height=512, .depth=1, .levels=1, .pkg_block_h=1}};
    nvn_tex_layout(&guest.d, &guest.L);
    CHECK(guest.L.size == bytes, "fixture covers the complete guest texture storage");
    NSHashTable *buffers = [NSHashTable weakObjectsHashTable];
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:512 height:512 mipmapped:NO];
    d.storageMode = MTLStorageModePrivate;
    id<MTLTexture> texture = [g_dev newTextureWithDescriptor:d]; res_add(texture);
    id<MTLBuffer> source = [g_dev newBufferWithLength:bytes options:MTLResourceStorageModePrivate]; res_add(source);
    NVNcopyRegion region = {0, 0, 0, 512, 512, 1};
    size_t peak = 0;
    for (unsigned i = 0; i < 64; i++) {
        // Hold feedback, not the GPU: the shared-event waiter must land the CPU
        // effects itself. No sleeps or scheduling race decide which path is tested.
        dispatch_semaphore_t release = dispatch_semaphore_create(0), callback = dispatch_semaphore_create(0);
        dispatch_async(g_feedback_q, ^{ dispatch_semaphore_wait(release, DISPATCH_TIME_FOREVER); });
        mtl_sub_begin();
        id e = blit();
        [e fillBuffer:source range:NSMakeRange(0, bytes) value:(uint8_t)(i + 1)];
        e = blit();   // order the dependent copy after the fill
        enc_b_copy_buffer_texture(e, source, 0, 2048, bytes, MTLSizeMake(512, 512, 1), texture, 0, 0, MTLOriginMake(0, 0, 0));
        mirror_readback(&guest, texture, 0, 0, &region, "test", 0);
        mirror_rb *rb = g_post[g_npost - 1].vis;
        [buffers addObject:(__bridge id)rb->buf];
        volatile uint32_t event = 0;
        post((post_op){.kind=1, .val=&event, .mode=1, .v=1});
        [g_commit_blocks addObject:^{ dispatch_semaphore_signal(callback); }];
        uint64_t fence = mtl_sub_commit();
        CHECK(mtl_wait(fence, 5000000000ull), "early waiter completes with feedback held");
        CHECK(event == 1, "event is visible when the early wait returns");
        int correct = 1;
        for (size_t j = 0; j < bytes; j++) if (mem[j] != i + 1) {
            fprintf(stderr, "readback %u byte %zu: expected %u, got %u\n", i, j, i + 1, mem[j]); correct = 0; break;
        }
        CHECK(correct, "all GPU-written bytes reach guest memory before the wait returns");
        dispatch_semaphore_signal(release);
        CHECK(!dispatch_semaphore_wait(callback, dispatch_time(DISPATCH_TIME_NOW, 5000000000ll)), "delayed feedback arrives");
        dispatch_sync(g_feedback_q, ^{});
        CHECK(event == 1, "late feedback does not repeat the completed effects");
        defer_drain(); res_commit();
        peak = MAX(peak, alive_bytes(buffers));
    }
    // A driver may retain its most recent command's objects. Earlier readbacks
    // must not accumulate: the unscoped path retains all 64 MiB on the waiter.
    CHECK(peak <= 2 * bytes, "retired readback storage remains bounded with the outer pool open");
    printf("post memory: peak retired %zu MiB, %d checks, %d failed\n", peak >> 20, checks, fails);
    res_remove(source); res_remove(texture); res_commit(); free(mem);
    return fails != 0;
} }

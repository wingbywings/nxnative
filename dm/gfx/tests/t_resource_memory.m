// A texture retained only by a descriptor/view cache must remain visible after
// residency removal. Repeated snapshots must not keep it alive after cache release.
#include "../nvn/nvn_mtl.m"
void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return bp_now(); }
static int checks, fails;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)
static unsigned owners_of(id resource) { @autoreleasepool {
    NSMapTable *map = [NSMapTable strongToStrongObjectsMapTable];
    mem_res_owners(map); mem_view_owners(map);
    return [[map objectForKey:resource] unsignedIntValue];
} }
int main(void) { @autoreleasepool {
    alarm(60); mtl_init();
    for (unsigned i = 0; i < 32; i++) {
        __weak id retired;
        @autoreleasepool {
            MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:128 height:128 mipmapped:NO];
            d.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
            id<MTLTexture> texture = [g_dev newTextureWithDescriptor:d];
            id<MTLTexture> view = [texture newTextureViewWithPixelFormat:MTLPixelFormatRGBA8Unorm_sRGB];
            retired = texture;
            res_add(texture); res_commit();
            CHECK(owners_of(texture) == MEM_RESIDENT, "new texture is resident only");
            tex_t t = {.serial = 1000000 + i}; mtl_tex_init(&t);
            mtl_reg_tex(1, &t, NULL);
            // Both descriptor aliases and the per-serial cache retain the same backing.
            os_unfair_lock_lock(&g_rmu);
            g_treg[1].view = (void *)CFBridgingRetain(view);
            g_treg[1].alt = (void *)CFBridgingRetain(view);
            g_treg[1].im = (void *)CFBridgingRetain(view);
            vc_key key = {0}; vc_put(t.serial, NULL, &key, view);
            os_unfair_lock_unlock(&g_rmu);
            CHECK(owners_of(texture) == (MEM_RESIDENT | MEM_VIEW), "view owners overlap the resident allocation");
            res_remove(texture); res_commit();
            CHECK(owners_of(texture) == (MEM_REMOVED | MEM_VIEW), "cache keeps a nonresident backing visible");
            res_add(texture); res_commit();
            CHECK(owners_of(texture) == (MEM_RESIDENT | MEM_VIEW), "readmission clears the removed classification");
            res_remove(texture); res_commit();
            mem_census(i);   // the actual report must also drain its temporary references
            mtl_tex_release(&t); mtl_reg_tex(1, NULL, NULL);
            // No GPU commands were encoded: satisfy the deferred host-reference fence.
            g_done = g_fence + 1; defer_drain(); res_commit();
        }
        @autoreleasepool { CHECK(retired == nil, "diagnostics do not pin a released cache backing"); }
    }
    printf("t_resource_memory: %d checks, %d failed\n", checks, fails);
    return fails != 0;
} }

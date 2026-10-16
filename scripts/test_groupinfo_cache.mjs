// Unit test: getGroupInfo cache (bridge/zalo.mjs) — additive, opt-in, LRU-bounded.
// cacheMs<=0 => NO caching (exact old behaviour). cacheMs>0 => reuse within TTL.
import { getGroupInfoCached, groupInfoCacheClear, groupInfoCacheSize } from "../bridge/zalo.mjs";

let fails = 0;
function check(name, cond, extra = "") {
  console.log((cond ? "OK  " : "FAIL") + "  " + name + (extra && !cond ? " :: " + extra : ""));
  if (!cond) fails++;
}

function makeApi() {
  let calls = 0;
  return {
    calls: () => calls,
    async getGroupInfo(ids) {
      calls += 1;
      return { gridInfoMap: { [ids[0]]: { name: "G", totalMember: 2 } }, _n: calls };
    },
  };
}

// --- cacheMs=0 (default) => every call hits the API (old behaviour) ---------
{
  groupInfoCacheClear();
  const api = makeApi();
  await getGroupInfoCached(api, "g1", { cacheMs: 0 });
  await getGroupInfoCached(api, "g1", { cacheMs: 0 });
  check("cacheMs=0 never caches", api.calls() === 2, `calls=${api.calls()}`);
  check("cacheMs=0 leaves cache empty", groupInfoCacheSize() === 0, `size=${groupInfoCacheSize()}`);
}

// --- absent opts => also no caching -----------------------------------------
{
  groupInfoCacheClear();
  const api = makeApi();
  await getGroupInfoCached(api, "g1");
  await getGroupInfoCached(api, "g1");
  check("absent opts never caches", api.calls() === 2, `calls=${api.calls()}`);
}

// --- cacheMs>0 => second call within TTL reuses -----------------------------
{
  groupInfoCacheClear();
  const api = makeApi();
  const a = await getGroupInfoCached(api, "g1", { cacheMs: 60000 });
  const b = await getGroupInfoCached(api, "g1", { cacheMs: 60000 });
  check("cached call hits API once", api.calls() === 1, `calls=${api.calls()}`);
  check("cached value identical", a === b);
  check("cache holds 1 entry", groupInfoCacheSize() === 1, `size=${groupInfoCacheSize()}`);
}

// --- TTL expiry causes a refetch --------------------------------------------
{
  groupInfoCacheClear();
  const api = makeApi();
  await getGroupInfoCached(api, "g1", { cacheMs: 1 });
  await new Promise((r) => setTimeout(r, 15)); // > 1ms TTL
  await getGroupInfoCached(api, "g1", { cacheMs: 1 });
  check("expired entry refetched", api.calls() === 2, `calls=${api.calls()}`);
}

// --- LRU cap: cacheMax bounds growth; oldest key evicted --------------------
{
  groupInfoCacheClear();
  const api = makeApi();
  await getGroupInfoCached(api, "g1", { cacheMs: 60000, cacheMax: 3 });
  await getGroupInfoCached(api, "g2", { cacheMs: 60000, cacheMax: 3 });
  await getGroupInfoCached(api, "g3", { cacheMs: 60000, cacheMax: 3 });
  await getGroupInfoCached(api, "g4", { cacheMs: 60000, cacheMax: 3 });
  check("LRU cap holds cacheMax entries", groupInfoCacheSize() === 3, `size=${groupInfoCacheSize()}`);
  const before = api.calls();
  // g1 was the oldest -> evicted -> refetch
  await getGroupInfoCached(api, "g1", { cacheMs: 60000, cacheMax: 3 });
  check("oldest key was evicted", api.calls() === before + 1, `calls=${api.calls()} before=${before}`);
}

// --- distinct groups are cached separately ----------------------------------
{
  groupInfoCacheClear();
  const api = makeApi();
  await getGroupInfoCached(api, "g1", { cacheMs: 60000 });
  await getGroupInfoCached(api, "g2", { cacheMs: 60000 });
  await getGroupInfoCached(api, "g1", { cacheMs: 60000 });
  check("distinct groups cached independently", api.calls() === 2, `calls=${api.calls()}`);
}

console.log();
if (fails) {
  console.log(`FAILS: ${fails}`);
  process.exit(1);
}
console.log("FAILS: none");

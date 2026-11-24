// The editor's Python runtime: Pyodide in a module worker, running the feed-refinery
// package (the wheel CI publishes next to this file) and bridge.py from it.
//
// Every Pyodide core file is fetched with its Subresource Integrity hash, so
// the browser refuses a file the CDN changed; Pyodide checks the packages it
// loads (micropip) against the hashes in its lock file, and micropip checks
// the wheels from PyPI against PyPI's.

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
const INTEGRITY = {
  "pyodide.mjs": "sha384-Lp79fMwxa4n2BLtugpUAoQMlbxIdg8iCUiP1r3h7nJLkQjwlXT1JzZTeHVL+f4Dg",
  "pyodide.asm.mjs": "sha384-/2281iaC0Iimw0B3Kagy1DBfZcJG3W4KYW+EmOqhsdY3fR9BJ88FE2Zt6y0KQKpa",
  "pyodide.asm.wasm": "sha384-y++qrQ72KPA1fTjLIuGMP+jppgGxjbLCgfdb0zMrnNnQVhxRUXTOTF6rTxNzllSu",
  "python_stdlib.zip": "sha384-xW3A5jmenkrPXbsgrIaB1FCJh9OuLT7FRMagsJMgtlhVc19k/mSy3+7nMaF/d767",
  "pyodide-lock.json": "sha384-o8SQQvpVlZnOl04+3dFTJipKxul9GbViVusA379wlOrfHSgxbzt0yPiqCTxOiYES",
};
const WHEEL = "/tmp/feed_refinery-0.0.0-py3-none-any.whl";

const plainFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (input, init = {}) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const name = url.startsWith(PYODIDE) ? url.slice(PYODIDE.length) : null;
  if (!name || !INTEGRITY[name]) return plainFetch(input, init);
  // Pyodide doesn't always pass a failed core file on: say so here.
  return plainFetch(input, { ...init, integrity: INTEGRITY[name] }).catch((error) => {
    postMessage({ type: "failed", error: `${name} didn't load or failed its integrity check (${error.message})` });
    throw error;
  });
};

async function importVerified(name) {
  const response = await fetch(PYODIDE + name);
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  const url = URL.createObjectURL(new Blob([await response.text()], { type: "text/javascript" }));
  try {
    return await import(url);
  } finally {
    URL.revokeObjectURL(url);
  }
}

const status = (text) => postMessage({ type: "status", text });

async function boot() {
  status("Loading Python…");
  const [{ loadPyodide }, asm] = await Promise.all([importVerified("pyodide.mjs"), importVerified("pyodide.asm.mjs")]);
  const pyodide = await loadPyodide({ indexURL: PYODIDE, createPyodideModule: asm.default });
  status("Installing the feed-refinery package…");
  await pyodide.loadPackage("micropip", { messageCallback: () => {} });
  const micropip = pyodide.pyimport("micropip");
  // CI publishes the wheel and the locked versions of what it needs from
  // PyPI (uv.lock's pyodide group) next to this file.
  const [wheel, requirements] = await Promise.all(
    ["feed_refinery.whl", "requirements.txt"].map(async (name) => {
      const response = await plainFetch(new URL(name, import.meta.url), { cache: "no-cache" });
      if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
      return response;
    }),
  );
  pyodide.FS.writeFile(WHEEL, new Uint8Array(await wheel.arrayBuffer()));
  const pins = (await requirements.text())
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  await micropip.install(pins, { deps: false });
  await micropip.install(`emfs:${WHEEL}`, { deps: false });
  return { pyodide, bridge: pyodide.pyimport("feed_refinery.site.editor.bridge") };
}

const ready = boot();
ready.then(
  () => postMessage({ type: "ready" }),
  (error) => postMessage({ type: "failed", error: String(error?.message || error) }),
);

let bodies = 0;
onmessage = async ({ data }) => {
  const { id, name, args } = data;
  const paths = [];
  try {
    const { pyodide, bridge } = await ready;
    if (name === "preview") {
      // Bodies travel as bytes and wait in Pyodide's file system, so
      // feedparser sees them exactly as fetched, encoding included.
      for (const got of Object.values(args.bodies)) {
        if (got.data) {
          got.path = `/tmp/body-${bodies++}`;
          pyodide.FS.writeFile(got.path, new Uint8Array(got.data));
          paths.push(got.path);
          delete got.data;
        }
      }
    }
    postMessage({ id, result: JSON.parse(bridge.call(name, JSON.stringify(args))) });
  } catch (error) {
    postMessage({ id, error: String(error?.message || error) });
  } finally {
    const { pyodide } = await ready.catch(() => ({}));
    for (const path of paths) pyodide?.FS.unlink(path);
  }
};

// ship-router: the single Cloudflare Worker that serves every site published with `ship`.
//
// It is deployed once (by `ship setup`) and never changes when you publish a page.
// Every hostname you publish to is attached to this Worker as a "Custom Domain",
// and the Worker looks up the file to serve in an R2 bucket using this layout:
//
//     request:  https://demo.example.com/reports/q3/chart.png
//     R2 key:   demo.example.com/reports/q3/chart.png
//
// So the bucket is just a folder per hostname. `env.SITES` is the R2 bucket,
// bound to this Worker under that name when it is uploaded (see cloudflare.py).

export default {
  async fetch(request, env) {
    // These are static pages: reading is the only thing anyone should do.
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { Allow: "GET, HEAD" },
      });
    }

    const url = new URL(request.url);

    // URLs encode spaces and accents ("%20"); R2 keys hold the real characters.
    let path;
    try {
      path = decodeURIComponent(url.pathname);
    } catch {
      return notFound(400, "Bad request");
    }

    // "/" and "/reports/q3/" mean "the index.html in that folder".
    let key = url.hostname + path;
    if (key.endsWith("/")) {
      key += "index.html";
    }

    const object = await env.SITES.get(key);

    if (object === null) {
      // "/reports/q3" (no trailing slash) might be a folder. Redirect to
      // "/reports/q3/" instead of serving its index.html directly, because
      // relative links like "chart.png" only resolve correctly with the slash.
      const folderIndex = await env.SITES.head(key + "/index.html");
      if (folderIndex !== null) {
        return Response.redirect(`${url.origin}${url.pathname}/${url.search}`, 301);
      }
      return notFound(404, "Not found");
    }

    // writeHttpMetadata copies the Content-Type that `ship` stored at upload time.
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("ETag", object.httpEtag);
    // "no-cache" means browsers must check back before reusing a copy, so a
    // re-published page shows up immediately (unchanged files still return 304).
    headers.set("Cache-Control", "no-cache");
    // Keep generated pages out of search engines.
    headers.set("X-Robots-Tag", "noindex, nofollow");

    return new Response(request.method === "HEAD" ? null : object.body, { headers });
  },
};

function notFound(status, message) {
  return new Response(message, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", "X-Robots-Tag": "noindex" },
  });
}

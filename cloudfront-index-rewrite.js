function handler(event) {
    var request = event.request;
    var uri = request.uri;

    // Directory-style request ("/evals", "/evals/") -> serve its index.html.
    // Anything with a file extension (feed.xml, 2026-07-05.json, style.css)
    // passes through unchanged.
    if (uri.endsWith('/')) {
        request.uri += 'index.html';
    } else if (!uri.includes('.')) {
        request.uri += '/index.html';
    }

    return request;
}

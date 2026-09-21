/*
 * WebView は blob: / data: の <a download> を自分ではダウンロードできない。
 * クリックを横取りして中身を base64 にし、Android 側で Download フォルダへ書き出す。
 */
(function () {
  if (window.__androidDownloadHookInstalled) { return; }
  window.__androidDownloadHookInstalled = true;

  function bail(err) {
    try { AndroidFileSaver.failed(String(err)); } catch (e) {}
  }

  document.addEventListener('click', function (ev) {
    var el = ev.target;
    var anchor = null;
    while (el && el !== document) {
      if (el.tagName === 'A' && el.hasAttribute('download')) { anchor = el; break; }
      el = el.parentNode;
    }
    if (!anchor) { return; }

    // クリック直後に anchor が DOM から外されるので、ここで同期的に読み取る
    var href = anchor.getAttribute('href') || '';
    var name = anchor.getAttribute('download') || 'download';
    if (href.lastIndexOf('blob:', 0) !== 0 && href.lastIndexOf('data:', 0) !== 0) { return; }

    ev.preventDefault();
    ev.stopPropagation();

    fetch(href).then(function (res) {
      return res.blob();
    }).then(function (blob) {
      var reader = new FileReader();
      reader.onload = function () {
        var url = String(reader.result || '');
        var comma = url.indexOf(',');
        if (comma < 0) { bail('unexpected data url'); return; }
        try {
          AndroidFileSaver.save(name, blob.type || 'application/octet-stream', url.slice(comma + 1));
        } catch (e) {
          bail(e);
        }
      };
      reader.onerror = function () { bail('read error'); };
      reader.readAsDataURL(blob);
    }).catch(bail);
  }, true);
})();

// Opens Plaid's window so the owner can sign in to a bank, then hands the result to the server.
// The bank password goes only to the bank, inside Plaid's window; this page never sees it.
(function () {
  var KEY = 'imperialBankLink';

  function backToMoney(message) {
    window.location.href = '/finance/?notice=' + encodeURIComponent(message || '');
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return { error: 'The server didn’t answer. Try again.' }; });
    });
  }

  function say(message) {
    var box = document.getElementById('bank-status');
    if (box) { box.textContent = message; box.hidden = false; }
  }

  function finish(linkId, publicToken, metadata) {
    say('Pulling your accounts…');
    var institution = metadata && metadata.institution ? metadata.institution.name : '';
    post('/finance/bank/connect', linkId ? { link_id: linkId } : { public_token: publicToken, institution: institution })
      .then(function (d) {
        try { window.localStorage.removeItem(KEY); } catch (e) {}
        if (d.error) { say(d.error); return; }
        backToMoney(d.notice);
      });
  }

  function start(token, linkId, receivedRedirectUri) {
    if (!window.Plaid) { say('Plaid’s window didn’t load. Check the connection and try again.'); return; }
    var options = {
      token: token,
      onSuccess: function (publicToken, metadata) { finish(linkId, publicToken, metadata); },
      onExit: function (err) {
        try { window.localStorage.removeItem(KEY); } catch (e) {}
        if (err) say('The bank sign-in stopped before it finished. Try again when you’re ready.');
        else if (receivedRedirectUri) backToMoney('');
      }
    };
    if (receivedRedirectUri) options.receivedRedirectUri = receivedRedirectUri;
    window.Plaid.create(options).open();
  }

  // Connect a new bank, or reconnect one that wants a fresh sign-in.
  window.imperialBankConnect = function (linkId) {
    say('Opening Plaid…');
    post('/finance/bank/link-token', linkId ? { link_id: linkId } : {}).then(function (d) {
      if (d.error) { say(d.error); return; }
      // Some banks (Chase among them) leave for their own page on a phone and come back to
      // /finance/bank/oauth, which picks up from here.
      try { window.localStorage.setItem(KEY, JSON.stringify({ token: d.link_token, linkId: linkId || null })); } catch (e) {}
      var box = document.getElementById('bank-status');
      if (box) box.hidden = true;
      start(d.link_token, linkId || null);
    });
  };

  // On the return page after a bank's own sign-in.
  window.imperialBankResume = function () {
    var saved = null;
    try { saved = JSON.parse(window.localStorage.getItem(KEY) || 'null'); } catch (e) {}
    if (!saved || !saved.token) { backToMoney('The bank sign-in timed out. Tap Connect a bank again.'); return; }
    start(saved.token, saved.linkId, window.location.href);
  };

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-bank-connect]');
    if (!button) return;
    event.preventDefault();
    window.imperialBankConnect(button.getAttribute('data-bank-connect') || null);
  });
})();

/*
 * כדי לשלוח הודעת עדכון לכל המשתמשים: ערכו את השדה "version" בקובץ version.json שבשורש הריפו ודחפו ל-main בגיטהאב — זה הכל, אין צורך בשינוי בבקאנד.
 * מגבלה ידועה: הבדיקה הזו פועלת רק כל עוד הטאב/האפליקציה פתוחים או ברקע בתהליך דפדפן חי;
 * זו איננה התראת Push אמיתית של מערכת ההפעלה (לכך דרוש מנגנון Web Push עם מפתחות VAPID ושרת לשליחת ה-push, שלא מומש כאן) — אפליקציה שנסגרה/נהרגה כליל לא תתעורר מזה בלבד.
 */
(function () {
  'use strict';

  var VERSION_URL_BASE = 'https://raw.githubusercontent.com/David-oved/college-chat/main/version.json';
  var CHECK_INTERVAL_MS = 5 * 60 * 1000;
  var STORAGE_KEY = 'app_version';
  var BANNER_ID = 'update-check-banner';
  var notifiedThisSession = false;

  function safeGetStorage(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function safeSetStorage(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {
      /* ignore */
    }
  }

  function showBanner(newVersion) {
    try {
      if (document.getElementById(BANNER_ID)) {
        return;
      }

      var banner = document.createElement('div');
      banner.id = BANNER_ID;
      banner.setAttribute('dir', 'rtl');
      banner.style.position = 'fixed';
      banner.style.bottom = '0';
      banner.style.left = '0';
      banner.style.right = '0';
      banner.style.zIndex = '2147483647';
      banner.style.display = 'flex';
      banner.style.alignItems = 'center';
      banner.style.justifyContent = 'center';
      banner.style.flexWrap = 'wrap';
      banner.style.gap = '12px';
      banner.style.padding = '12px 16px';
      banner.style.margin = '0';
      banner.style.background = '#1f2430';
      banner.style.color = '#ffffff';
      banner.style.fontFamily = 'Arial, Helvetica, sans-serif';
      banner.style.fontSize = '15px';
      banner.style.boxShadow = '0 -2px 10px rgba(0,0,0,0.35)';
      banner.style.borderRadius = '12px 12px 0 0';
      banner.style.boxSizing = 'border-box';

      var text = document.createElement('span');
      text.textContent = '🔄 גרסה חדשה זמינה';
      text.style.fontWeight = 'bold';

      var updateBtn = document.createElement('button');
      updateBtn.type = 'button';
      updateBtn.textContent = 'עדכן עכשיו';
      updateBtn.style.background = '#4f7cff';
      updateBtn.style.color = '#ffffff';
      updateBtn.style.border = 'none';
      updateBtn.style.borderRadius = '8px';
      updateBtn.style.padding = '8px 16px';
      updateBtn.style.fontSize = '14px';
      updateBtn.style.fontWeight = 'bold';
      updateBtn.style.cursor = 'pointer';

      var dismissBtn = document.createElement('button');
      dismissBtn.type = 'button';
      dismissBtn.textContent = '✕';
      dismissBtn.setAttribute('aria-label', 'סגור');
      dismissBtn.style.background = 'transparent';
      dismissBtn.style.color = '#ffffff';
      dismissBtn.style.border = 'none';
      dismissBtn.style.fontSize = '16px';
      dismissBtn.style.cursor = 'pointer';
      dismissBtn.style.padding = '4px 8px';

      updateBtn.addEventListener('click', function () {
        performUpdate(newVersion);
      });

      dismissBtn.addEventListener('click', function () {
        try {
          if (banner.parentNode) {
            banner.parentNode.removeChild(banner);
          }
        } catch (e) {
          /* ignore */
        }
      });

      banner.appendChild(text);
      banner.appendChild(updateBtn);
      banner.appendChild(dismissBtn);

      (document.body || document.documentElement).appendChild(banner);
    } catch (e) {
      /* never let banner creation break the host page */
    }
  }

  function performUpdate(newVersion) {
    try {
      var unregisterPromise = Promise.resolve();
      if ('serviceWorker' in navigator && navigator.serviceWorker.getRegistrations) {
        unregisterPromise = navigator.serviceWorker
          .getRegistrations()
          .then(function (registrations) {
            return Promise.all(
              registrations.map(function (reg) {
                try {
                  return reg.unregister();
                } catch (e) {
                  return Promise.resolve();
                }
              })
            );
          })
          .catch(function () {
            /* ignore */
          });
      }

      var cachesPromise = Promise.resolve();
      if (typeof caches !== 'undefined' && caches.keys) {
        cachesPromise = caches
          .keys()
          .then(function (keys) {
            return Promise.all(
              keys.map(function (key) {
                try {
                  return caches.delete(key);
                } catch (e) {
                  return Promise.resolve();
                }
              })
            );
          })
          .catch(function () {
            /* ignore */
          });
      }

      Promise.all([unregisterPromise, cachesPromise])
        .catch(function () {
          /* ignore */
        })
        .then(function () {
          safeSetStorage(STORAGE_KEY, newVersion);
          try {
            location.reload();
          } catch (e) {
            /* ignore */
          }
        });
    } catch (e) {
      try {
        safeSetStorage(STORAGE_KEY, newVersion);
        location.reload();
      } catch (e2) {
        /* ignore */
      }
    }
  }

  function maybeNotify(newVersion) {
    try {
      if (notifiedThisSession) {
        return;
      }
      if (document.hidden !== true) {
        return;
      }
      if (typeof Notification === 'undefined') {
        return;
      }

      var fire = function () {
        try {
          new Notification('🔄 גרסה חדשה זמינה', {
            body: 'עדכן עכשיו'
          });
        } catch (e) {
          /* ignore */
        }
      };

      if (Notification.permission === 'granted') {
        notifiedThisSession = true;
        fire();
      } else if (Notification.permission === 'default') {
        notifiedThisSession = true;
        Notification.requestPermission()
          .then(function (permission) {
            if (permission === 'granted') {
              fire();
            }
          })
          .catch(function () {
            /* ignore */
          });
      }
      /* if 'denied', do nothing and don't ask again this session */
    } catch (e) {
      /* never let notification logic break the host page */
    }
  }

  function checkForUpdate() {
    try {
      var url = VERSION_URL_BASE + '?t=' + Date.now();
      fetch(url, { cache: 'no-store' })
        .then(function (response) {
          if (!response || !response.ok) {
            return null;
          }
          return response.json();
        })
        .then(function (data) {
          if (!data || !data.version) {
            return;
          }

          var fetchedVersion = data.version;
          var storedVersion = safeGetStorage(STORAGE_KEY);

          if (storedVersion === null) {
            safeSetStorage(STORAGE_KEY, fetchedVersion);
            return;
          }

          if (storedVersion !== fetchedVersion) {
            showBanner(fetchedVersion);
            maybeNotify(fetchedVersion);
          }
        })
        .catch(function () {
          /* network failure: silently no-op */
        });
    } catch (e) {
      /* never let this script's own errors break the host page */
    }
  }

  try {
    checkForUpdate();
    setInterval(checkForUpdate, CHECK_INTERVAL_MS);
    document.addEventListener('visibilitychange', function () {
      try {
        if (document.visibilityState === 'visible') {
          checkForUpdate();
        }
      } catch (e) {
        /* ignore */
      }
    });
  } catch (e) {
    /* never let setup errors break the host page */
  }
})();

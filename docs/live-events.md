# Live events: fans and gates

Two different problems. Fans refreshing is a read problem, and a cache solves most of it. Gates are a write problem with a person standing there, and no cache helps.

## Fans refreshing a page

**Built and measured.** Numbers are in the measurements section.

| Question | Answer |
|---|---|
| What is cached? | The anonymous event page and its availability numbers, for one second, in nginx |
| What is never cached? | Gate scans, the operator console, and anything with personal data. The path pattern allows only those two read paths, and only GET |
| What do fans see? | Numbers up to about a second old, or older while Django is down |
| The cache expires under a spike? | `proxy_cache_lock` lets one request through per key. The others get the stale copy (`use_stale updating`), and the refresh runs in the background |
| After the lock timeout? | Per [nginx's docs](https://nginx.org/en/docs/http/ngx_http_proxy_module.html), a request that waited past the timeout goes to Django and its reply is not cached. That is why the lock timeout is 3 s and stale serving comes first |
| Django stops? | Fans keep the last known page. A page nobody has requested yet has no copy, and fails until Django returns |
| A personal page by mistake? | It would need that path. `Set-Cookie` is stripped and ignored on those paths, so a view that set a cookie there would lose it, not leak it. Keep personal data out of these paths: [why personalised replies break collapsing](https://www.fastly.com/documentation/guides/concepts/edge-state/cache/request-collapsing/) |

**Not covered:**
- One nginx is not a CDN. A real on-sale needs one, in front of this.
- The load generator ran on the same machine. Real fans bring slow networks and TLS, neither of which was tested.
- Refreshing is the easy part of an on-sale. [Ticketmaster's own account](https://business.ticketmaster.com/press-release/taylor-swift-the-eras-tour-onsale-explained/) describes tickets lost in carts, not only slow pages.

## Buying tickets (not built)

Encore has no checkout, holds or payments. If an on-sale is the job, the design questions are:

- **Overselling.** A conditional update on stock inside a transaction, the same pattern as admission here. Holds with an expiry.
- **A payment callback after the hold expired.** Needs a decision: honour it or refund it.
- **Fairness.** Edge rate limits cannot keep order, which is [why waiting rooms exist](https://www.infoq.com/podcasts/handling-high-demand-ticket-on-sales-parra-pellegrino/). I would buy this first ([Cloudflare](https://developers.cloudflare.com/waiting-room/about/), [Queue-it](https://queue-it.com/developers/how-queue-it-works/)), and validate its signed token on protected paths, rather than build one.
- **Alerts** that change priority when an on-sale is live.

## Gates when the network or the backend is down

**The server side is built and tested. There is no scanner app.** `/api/v1/events/<id>/manifest` and `/api/v1/scans/offline` are what a device would use. Nothing has run on a real device or at a real venue.

```
Before doors    Device downloads the manifest: ticket id, status, used. No names or emails.
Doors open      Device decides by itself and remembers what it admitted.
Network back    Device uploads each scan with its own id and original time. Safe to repeat.
Server          Accepts the first scan per ticket, and records anything else for a person.
```

| What happens | Attendee | Server | Test |
|---|---|---|---|
| Normal online scan | Admitted | Admitted, outbox message | `test_same_ticket_scanned_at_many_gates_admits_exactly_one` |
| Second person with the same ticket, same gate | Refused, the device remembers | Nothing new | Device logic, not built |
| Same ticket at two offline gates | **Both admitted. This risk is accepted** | First upload wins, second is a conflict | `test_two_offline_gates_admitting_one_ticket_become_a_conflict_to_review` |
| Ticket refunded after the device synced | **Admitted**, the device could not know | Conflict `ticket_refunded`, no check-in sent | `test_ticket_refunded_after_the_device_synced_becomes_a_conflict_not_a_normal_entry` |
| Ticket the server never heard of | Whatever the device decided | Conflict `unknown_ticket` | `test_unknown_ticket_scanned_offline_is_kept_for_review` |
| Upload repeated after a lost reply | | `duplicate_upload`, nothing changes | `test_upload_keeps_the_scan_time_writes_one_outbox_message_and_can_be_repeated` |
| Device clock far ahead | | Rejected, kept by the device | `test_upload_rejects_future_times_and_junk_without_losing_the_good_scans` |
| Same scan id for two tickets | | Rejected, nobody is admitted | `test_one_scan_id_used_for_two_tickets_admits_nobody_new` |

Duplicates while offline are normal for this kind of system: [pretix](https://docs.pretix.eu/en/latest/api/resources/checkin.html) and [SimpleTix](https://help.simpletix.com/docs/simpletix-mobile-apps/simpletix-organizer-app/offline-scanning) both document them. An organizer who cannot accept that needs an on-site server on venue Wi-Fi, as [pretix's proxy](https://marketplace.pretix.eu/products/pretixscan-proxy) does, which gives one scan per ticket across gates. That is not built here.

**Open decisions an organizer owns:** fail open or closed when a device has no manifest. How old a manifest may be. Re-entry. What staff do on a duplicate. How long a lost device's data matters: it holds ticket ids only, not names.

## Cashless and staff

Not touched here. I found no good public design for chip-level stored value, and I would not guess at one. It would be the first thing to learn from the team.

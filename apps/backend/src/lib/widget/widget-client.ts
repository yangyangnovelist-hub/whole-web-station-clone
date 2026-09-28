/* eslint-disable */
/**
 * Browser-side instant quote widget. This function is serialized with
 * Function.prototype.toString() by GET /packoasis/widget.js, so it must be
 * fully self-contained: no imports and no references to module scope.
 *
 * Embed on any storefront page (mirror HTML or Next.js):
 *   <script src="https://<backend>/packoasis/widget.js"
 *           data-publishable-key="pk_..." defer></script>
 */
export function packoasisWidget(win: any, doc: Document, boot: any) {
  if (win.__packoasisWidget) {
    return
  }
  win.__packoasisWidget = true

  const script: HTMLScriptElement | null =
    (doc.currentScript as HTMLScriptElement | null) ||
    doc.querySelector('script[src*="/packoasis/widget.js"]')
  if (!script) {
    return
  }

  const scriptUrl = new URL(script.src, win.location.href)
  const API = (script.dataset.backend || scriptUrl.origin).replace(/\/$/, "")
  const KEY = script.dataset.publishableKey || ""
  const COUNTRY = (script.dataset.country || "us").toLowerCase()
  // Stay out of the way during checkout and in the account area. Read from
  // the current path: the Next.js storefront navigates client-side.
  const isQuiet = () =>
    /\/(checkout|account|cart|order)(\/|$)/.test(win.location.pathname)
  const BUTTON = script.dataset.button !== "false"
  const nav: any = win.navigator
  const TRACKING =
    script.dataset.tracking !== "false" &&
    nav.doNotTrack !== "1" &&
    nav.globalPrivacyControl !== true
  const catalog = boot.catalog
  const types: any[] = catalog.product_types
  const typeById: Record<string, any> = {}
  types.forEach((type) => (typeById[type.id] = type))
  const finishLabels: Record<string, string> = {}
  catalog.finishes.forEach(
    (finish: any) => (finishLabels[finish.id] = finish.label)
  )
  const addonLabels: Record<string, string> = {}
  catalog.addons.forEach((addon: any) => (addonLabels[addon.id] = addon.label))

  // ---------------------------------------------------------------- helpers
  const esc = (value: any) =>
    String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
  const usd = (amount: number) =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: String(catalog.currency_code || "usd").toUpperCase(),
      minimumFractionDigits: 2,
      maximumFractionDigits: amount < 1 ? 3 : 2,
    }).format(amount)
  const num = (value: number) => Number(value).toLocaleString("en-US")

  function randomId() {
    const alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    const bytes = new Uint8Array(18)
    win.crypto.getRandomValues(bytes)
    return Array.from(bytes, (byte: number) => alphabet[byte & 63]).join("")
  }

  let visitorId = ""
  try {
    visitorId = win.localStorage.getItem("po_vid") || ""
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(visitorId)) {
      visitorId = randomId()
      win.localStorage.setItem("po_vid", visitorId)
    }
  } catch (e) {
    visitorId = randomId()
  }

  const page = () => ({
    url: win.location.href.slice(0, 500),
    path: win.location.pathname.slice(0, 300),
    title: doc.title.slice(0, 200),
  })

  function typeForPath(path: string) {
    const lower = path.toLowerCase()
    for (const hint of catalog.page_hints) {
      if (new RegExp(hint.pattern).test(lower) && typeById[hint.product_type]) {
        return hint.product_type
      }
    }
    return null
  }
  let pageType = typeForPath(win.location.pathname)

  // --------------------------------------------------------------- tracking
  const queue: any[] = []
  let flushTimer: any = null
  function flush() {
    if (!queue.length) {
      return
    }
    const body = JSON.stringify({ v: visitorId, e: queue.splice(0, 20) })
    const url = API + "/packoasis/events"
    try {
      if (nav.sendBeacon && nav.sendBeacon(url, body)) {
        return
      }
    } catch (e) {}
    win
      .fetch(url, {
        method: "POST",
        body,
        keepalive: true,
        mode: "no-cors",
        headers: { "Content-Type": "text/plain" },
      })
      .catch(() => {})
  }
  function track(type: string, extra?: any) {
    if (!TRACKING) {
      return
    }
    const p = page()
    queue.push(
      Object.assign(
        {
          t: type,
          u: p.url,
          p: p.path,
          ti: p.title,
          pt: pageType || undefined,
        },
        extra || {}
      )
    )
    if (queue.length >= 10) {
      flush()
    } else if (!flushTimer) {
      flushTimer = setTimeout(() => {
        flushTimer = null
        flush()
      }, 4000)
    }
  }
  win.addEventListener("pagehide", flush)
  doc.addEventListener("visibilitychange", () => {
    if (doc.visibilityState === "hidden") {
      flush()
    }
  })
  // Back from the checkout redirect via the bfcache: the widget kept
  // submitting=true, so re-enable the contact form for a revised order.
  win.addEventListener("pageshow", (event: any) => {
    if (event.persisted && state.submitting) {
      state.submitting = false
      syncSubmit()
    }
  })

  const params = new URLSearchParams(win.location.search)
  const utm: Record<string, string> = {}
  ;[
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
  ].forEach((key) => {
    const value = params.get(key)
    if (value) {
      utm[key] = value.slice(0, 100)
    }
  })
  track("page_view", {
    r: doc.referrer ? doc.referrer.slice(0, 500) : undefined,
    d: Object.keys(utm).length ? { utm } : undefined,
  })
  let maxScroll = 0
  win.addEventListener(
    "scroll",
    () => {
      const height = doc.documentElement.scrollHeight - win.innerHeight
      const depth = height > 0 ? Math.round((win.scrollY / height) * 100) : 100
      if (depth >= maxScroll + 50 || (depth >= 90 && maxScroll < 90)) {
        maxScroll = depth
        track("scroll_depth", { d: { depth } })
      }
    },
    { passive: true }
  )

  // -------------------------------------------------------------------- API
  async function api(path: string, body?: any) {
    const res = await win.fetch(API + path, {
      method: body ? "POST" : "GET",
      headers: {
        "content-type": "application/json",
        "x-publishable-api-key": KEY,
      },
      body: body ? JSON.stringify(body) : undefined,
      credentials: "omit",
    })
    const json = await res.json().catch(() => ({}))
    if (!res.ok) {
      throw new Error(json.message || "Something went wrong, please try again.")
    }
    return json
  }

  // ------------------------------------------------------------------ state
  const state: any = {
    step: "configure",
    typeId: pageType || "mailer-box",
    dims: [],
    unit: "in",
    quantity: 0,
    material: "",
    print: "",
    finishes: [],
    addons: [],
    rush: false,
    quote: null,
    loading: false,
    error: "",
    aiBusy: false,
    aiNote: "",
    submitting: false,
    result: null,
  }

  function resetForType(typeId: string) {
    const type = typeById[typeId] || types[0]
    state.typeId = type.id
    state.dims = type.default_dimensions.slice()
    state.unit = "in"
    state.quantity = type.moq
    state.material = type.default_material
    state.print = type.default_print
    state.finishes = []
    state.addons = []
    state.rush = false
  }
  resetForType(state.typeId)

  function specs() {
    return {
      product_type: state.typeId,
      dimensions: state.dims.map((value: number) => Number(value)),
      unit: state.unit,
      quantity: Number(state.quantity),
      material: state.material,
      print: state.print,
      finishes: state.finishes,
      addons: state.addons,
      rush: state.rush,
    }
  }

  // ------------------------------------------------------------------ view
  // Host pages can lift the floating button above their own sticky bars by
  // setting --po-fab-offset on <html> (custom properties survive all:initial).
  const css = `
:host{all:initial}
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.fab{position:fixed;right:20px;bottom:calc(20px + var(--po-fab-offset, 0px));z-index:2147483000;background:#1a7f45;color:#fff;border:0;border-radius:999px;padding:14px 20px;font-size:15px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.2);cursor:pointer;display:flex;gap:8px;align-items:center}
.fab:hover{background:#156b3a}
.fab:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #40C173;outline-offset:2px}
.backdrop{position:fixed;inset:0;background:rgba(17,24,39,.55);z-index:2147483001;display:flex;align-items:center;justify-content:center;padding:16px}
.dialog{background:#fff;color:#1f2430;width:100%;max-width:880px;max-height:calc(100vh - 32px);overflow:auto;border-radius:14px;box-shadow:0 24px 64px rgba(0,0,0,.3);border-top:4px solid #40C173}
.head{display:flex;justify-content:space-between;align-items:flex-start;padding:18px 22px 6px}
.head h2{margin:0;font-size:20px}
.head p{margin:4px 0 0;color:#606577;font-size:13px}
.x{background:none;border:0;font-size:26px;line-height:1;cursor:pointer;color:#606577;padding:2px 6px}
.body{display:grid;grid-template-columns:1.25fr 1fr;gap:20px;padding:12px 22px 22px}
.col{min-width:0}
label{display:block;font-size:12px;font-weight:600;color:#3d4250;margin:12px 0 4px;text-transform:uppercase;letter-spacing:.03em}
input,select,textarea{width:100%;border:1px solid #cfd5d2;border-radius:8px;padding:9px 10px;font-size:15px;color:#1f2430;background:#fff}
textarea{min-height:64px;resize:vertical}
.row{display:flex;gap:8px}
.row>*{flex:1}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{border:1px solid #cfd5d2;background:#fff;border-radius:999px;padding:5px 10px;font-size:13px;cursor:pointer;color:#1f2430}
.chip[aria-pressed=true]{background:#e8f6ee;border-color:#1a7f45;color:#12542e;font-weight:600}
.checks{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:4px}
.checks label{display:flex;gap:6px;align-items:center;text-transform:none;font-weight:500;font-size:14px;letter-spacing:0;margin:0}
.checks input{width:auto}
.ai{background:#f5f8f6;border:1px dashed #b9d8c5;border-radius:10px;padding:10px 12px;margin-top:14px}
.ai .row{align-items:flex-end}
.ai button{flex:0 0 auto}
.price{background:#f5f8f6;border-radius:12px;padding:16px;position:sticky;top:0}
.total{font-size:32px;font-weight:800;margin:4px 0 0}
.unit{color:#3d4250;font-size:14px}
.meta{font-size:13px;color:#3d4250;margin:10px 0;line-height:1.5}
.tiers{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
.tiers td{padding:6px 4px;border-top:1px solid #e1e6e3}
.tiers tr{cursor:pointer}
.tiers tr:hover td{background:#eaf4ee}
.save{color:#1a7f45;font-weight:600;text-align:right}
.warn{background:#fff7e6;border-radius:8px;padding:8px 10px;font-size:12px;color:#7a4b00;margin-top:8px}
.err{background:#fdecec;border-radius:8px;padding:8px 10px;font-size:13px;color:#8a1c1c;margin-top:8px}
.cta{width:100%;background:#1a7f45;color:#fff;border:0;border-radius:10px;padding:14px;font-size:16px;font-weight:700;cursor:pointer;margin-top:10px}
.cta:hover{background:#156b3a}
.cta[disabled]{opacity:.6;cursor:wait}
.ghost{background:none;border:1px solid #cfd5d2;color:#1f2430;border-radius:10px;padding:10px 14px;font-size:14px;cursor:pointer}
.small{font-size:12px;color:#606577;line-height:1.5}
.trust{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:#3d4250;margin-top:10px}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.done{padding:10px 22px 26px}
.badge{display:inline-block;background:#e8f6ee;color:#12542e;border-radius:999px;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.03em}
@media (max-width:720px){.backdrop{padding:0}.dialog{max-height:100vh;height:100%;border-radius:0}.body{grid-template-columns:1fr}.price{position:static}.fab{right:12px;bottom:calc(12px + var(--po-fab-offset, 0px));padding:12px 16px}}
`

  const host = doc.createElement("div")
  host.id = "packoasis-quote-widget"
  const root = host.attachShadow({ mode: "open" })
  const style = doc.createElement("style")
  style.textContent = css
  root.appendChild(style)
  const container = doc.createElement("div")
  root.appendChild(container)
  let lastFocus: any = null

  function mount() {
    if (!host.isConnected) {
      doc.body.appendChild(host)
    }
  }

  function fabHtml() {
    return BUTTON && !isQuiet() && !state.open
      ? `<button class="fab" data-act="open" aria-haspopup="dialog">
<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M13 2 3 14h7l-1 8 10-12h-7z"/></svg>Instant quote</button>`
      : ""
  }

  function fieldHtml() {
    const type = typeById[state.typeId]
    const typeOptions = types
      .map(
        (t) =>
          `<option value="${esc(t.id)}" ${t.id === type.id ? "selected" : ""}>${esc(t.label)}${
            t.instant ? "" : " (reviewed quote)"
          }</option>`
      )
      .join("")
    const dims = type.dimension_labels
      .map(
        (
          label: string,
          index: number
        ) => `<div><label for="po-d${index}">${esc(label)} (${state.unit})</label>
<input id="po-d${index}" data-dim="${index}" type="number" inputmode="decimal" min="0" step="0.125" value="${esc(state.dims[index])}"></div>`
      )
      .join("")
    const moq = type.moq
    const qtyChips = [moq, moq * 2, moq * 5, moq * 10]
      .filter((q) => q <= type.max_quantity)
      .map(
        (q) =>
          `<button type="button" class="chip" data-qty="${q}" aria-pressed="${
            Number(state.quantity) === q
          }">${num(q)}</button>`
      )
      .join("")
    const printOptions = type.print_options
      .map(
        (id: string) =>
          `<option value="${esc(id)}" ${id === state.print ? "selected" : ""}>${esc(
            catalog.print_options[id]
          )}</option>`
      )
      .join("")
    const materials = type.materials
      .map(
        (m: any) =>
          `<option value="${esc(m.id)}" ${m.id === state.material ? "selected" : ""}>${esc(m.label)}</option>`
      )
      .join("")
    const finishes = type.finishes.length
      ? `<label>Finishes</label><div class="checks">${type.finishes
          .map(
            (id: string) =>
              `<label><input type="checkbox" data-finish="${esc(id)}" ${
                state.finishes.indexOf(id) >= 0 ? "checked" : ""
              }>${esc(finishLabels[id] || id)}</label>`
          )
          .join("")}</div>`
      : ""
    const addons = type.addons.length
      ? `<label>Add-ons</label><div class="checks">${type.addons
          .map(
            (id: string) =>
              `<label><input type="checkbox" data-addon="${esc(id)}" ${
                state.addons.indexOf(id) >= 0 ? "checked" : ""
              }>${esc(addonLabels[id] || id)}</label>`
          )
          .join("")}</div>`
      : ""

    return `<label for="po-type">Packaging</label><select id="po-type" data-field="type">${typeOptions}</select>
${
  type.dimension_labels.length
    ? `<div class="row">${dims}</div><div class="chips"><button type="button" class="chip" data-unit="in" aria-pressed="${
        state.unit === "in"
      }">inches</button><button type="button" class="chip" data-unit="cm" aria-pressed="${
        state.unit === "cm"
      }">cm</button></div>`
    : ""
}
<label for="po-qty">Quantity <span class="small">(minimum ${num(moq)})</span></label>
<input id="po-qty" data-field="quantity" type="number" inputmode="numeric" min="${moq}" step="1" value="${esc(state.quantity)}">
<div class="chips">${qtyChips}</div>
<div class="row"><div><label for="po-print">Print</label><select id="po-print" data-field="print">${printOptions}</select></div>
<div><label for="po-material">Material</label><select id="po-material" data-field="material">${materials}</select></div></div>
${finishes}${addons}
<div class="checks" style="margin-top:12px"><label><input type="checkbox" data-field="rush" ${
      state.rush ? "checked" : ""
    }>Rush production (about 40% faster)</label></div>
<div class="ai"><label for="po-describe" style="margin-top:0">Or describe your project${
      boot.ai ? ' <span class="badge">AI</span>' : ""
    }</label>
<div class="row"><textarea id="po-describe" placeholder="e.g. 2,000 white mailer boxes 9x6x3 in, full color outside, soft-touch with gold foil logo"></textarea>
<button type="button" class="ghost" data-act="describe" ${state.aiBusy ? "disabled" : ""}>${
      state.aiBusy ? "Reading..." : "Fill in"
    }</button></div>${state.aiNote ? `<p class="small">${esc(state.aiNote)}</p>` : ""}</div>`
  }

  function priceHtml() {
    const quote = state.quote
    const type = typeById[state.typeId]
    if (!quote) {
      return `<div class="price"><p class="unit">${state.loading ? "Calculating..." : "Adjust the specs to see your price."}</p>${
        state.error ? `<div class="err">${esc(state.error)}</div>` : ""
      }</div>`
    }
    const tiers = quote.tiers
      .map(
        (tier: any) =>
          `<tr data-qty="${tier.quantity}" title="Use ${num(tier.quantity)} pcs"><td>${num(tier.quantity)} pcs</td><td>${usd(
            tier.unit_price
          )}/pc</td><td>${usd(tier.total)}</td><td class="save">${
            tier.savings_pct > 0 ? `save ${tier.savings_pct}%` : ""
          }</td></tr>`
      )
      .join("")
    return `<div class="price" aria-live="polite">
<div class="small">${quote.instant ? "Your instant price" : "Indicative budget"}${
      quote.freight_included ? " · freight included" : ""
    }</div>
<p class="total">${usd(quote.total)}</p>
<div class="unit">${usd(quote.unit_price)} per piece · ${num(quote.quantity)} pcs${state.loading ? " · updating..." : ""}</div>
<div class="meta">Production ${quote.lead_time.production_days[0]}-${quote.lead_time.production_days[1]} business days${
      quote.lead_time.rush ? " (rush)" : ""
    } after proof<br>Estimated delivery ${esc(quote.estimated_delivery.from)} to ${esc(
      quote.estimated_delivery.to
    )}<br>Price locked until ${esc(quote.valid_until)}</div>
<table class="tiers" aria-label="Volume pricing">${tiers}</table>
${quote.warnings.length ? `<div class="warn">${quote.warnings.map(esc).join("<br>")}</div>` : ""}
${state.error ? `<div class="err">${esc(state.error)}</div>` : ""}
<button class="cta" data-act="next" ${state.loading ? "disabled" : ""}>${
      quote.instant
        ? `Order now · ${usd(quote.total)}`
        : "Request reviewed quote"
    }</button>
<div class="trust"><span>✓ Free digital proof</span><span>✓ No payment until you confirm</span><span>✓ ${esc(
      type.category
    )} specialists</span></div></div>`
  }

  function submitLabel() {
    const quote = state.quote
    return state.submitting
      ? "Locking your price..."
      : quote && quote.instant
        ? `Continue to checkout · ${usd(quote.total)}`
        : "Send my request"
  }

  function contactHtml() {
    const quote = state.quote
    const countries = [
      ["us", "United States"],
      ["ca", "Canada"],
      ["gb", "United Kingdom"],
      ["au", "Australia"],
    ]
    const listed = countries.some((c) => c[0] === COUNTRY)
    const countryOptions =
      countries
        .map(
          (c) =>
            `<option value="${c[0]}" ${c[0] === COUNTRY ? "selected" : ""}>${c[1]}</option>`
        )
        .join("") +
      `<option value="xx" ${listed ? "" : "selected"}>Other country</option>`
    return `<div class="body"><div class="col">
<form data-form="contact" novalidate>
<label for="po-name">Full name</label><input id="po-name" name="name" autocomplete="name" required>
<label for="po-email">Work email</label><input id="po-email" name="email" type="email" autocomplete="email" required>
<div class="row"><div><label for="po-company">Company</label><input id="po-company" name="company" autocomplete="organization"></div>
<div><label for="po-phone">Phone</label><input id="po-phone" name="phone" type="tel" autocomplete="tel"></div></div>
<div class="row"><div><label for="po-website">Website</label><input id="po-website" name="website" placeholder="yourbrand.com" autocomplete="url"></div>
<div><label for="po-country">Ship to</label><select id="po-country" name="country">
${countryOptions}</select></div></div>
<label for="po-notes">Anything we should know? <span class="small">(optional)</span></label><textarea id="po-notes" name="notes" placeholder="Artwork status, deadline, delivery details"></textarea>
<div class="hp" aria-hidden="true"><label>Leave empty<input name="hp" tabindex="-1" autocomplete="off"></label></div>
${state.error ? `<div class="err">${esc(state.error)}</div>` : ""}
<button class="cta" type="submit" ${state.submitting ? "disabled" : ""}>${submitLabel()}</button>
<p class="small">We email your quote and may follow up about this request; you can opt out with one click. By continuing you agree to the Terms of Service and Privacy Policy.</p>
<button type="button" class="ghost" data-act="back">← Edit specs</button>
</form></div><div class="col" data-slot="summary">${quote ? summaryHtml(quote) : ""}</div></div>`
  }

  function summaryHtml(quote: any) {
    return `<div class="price"><div class="small">${esc(quote.product_label)}</div><p class="total">${usd(
      quote.total
    )}</p><div class="unit">${usd(quote.unit_price)} per piece · ${num(quote.quantity)} pcs</div>
<div class="meta">${esc(quote.spec_labels.dimensions)}<br>${esc(quote.spec_labels.material)}<br>${esc(
      quote.spec_labels.print
    )}${quote.spec_labels.finishes.length ? `<br>${esc(quote.spec_labels.finishes.join(", "))}` : ""}<br>Delivery ${esc(
      quote.estimated_delivery.from
    )} to ${esc(quote.estimated_delivery.to)}</div></div>`
  }

  function doneHtml() {
    const result = state.result
    return `<div class="done"><h3>Thanks, we're on it.</h3><p>${esc(
      result && result.message
    )}</p><p class="small">Reference ${esc(
      result && result.rfq_id
        ? String(result.rfq_id).slice(-8).toUpperCase()
        : ""
    )}. A copy is on its way to your inbox.</p><button class="cta" data-act="close">Close</button></div>`
  }

  function render() {
    let html = fabHtml()
    if (state.open) {
      const title =
        state.step === "contact"
          ? "Where should we send your order?"
          : state.step === "done"
            ? "Request received"
            : "Instant packaging quote"
      html += `<div class="backdrop" data-act="backdrop"><div class="dialog" role="dialog" aria-modal="true" aria-labelledby="po-title">
<div class="head"><div><h2 id="po-title">${title}</h2><p>${
        state.step === "configure"
          ? "Real prices in seconds. Order in about two minutes."
          : state.step === "contact"
            ? "Your price is locked for 14 days. Pay only when you confirm at checkout."
            : ""
      }</p></div><button class="x" data-act="close" aria-label="Close">×</button></div>
${
  state.step === "configure"
    ? `<div class="body"><div class="col" data-slot="form">${fieldHtml()}</div><div class="col" data-slot="price">${priceHtml()}</div></div>`
    : state.step === "contact"
      ? contactHtml()
      : doneHtml()
}</div></div>`
    }
    container.innerHTML = html
  }

  function renderPrice() {
    // Late estimate responses must never re-render (and wipe) the contact form.
    const slot = container.querySelector('[data-slot="price"]')
    if (slot) {
      slot.innerHTML = priceHtml()
      return
    }
    const summary = container.querySelector('[data-slot="summary"]')
    if (summary && state.quote) {
      summary.innerHTML = summaryHtml(state.quote)
      syncSubmit()
    }
  }

  // Keep the contact step's submit button in line with state.quote.
  function syncSubmit() {
    const submit: any = container.querySelector(
      '[data-form="contact"] [type="submit"]'
    )
    if (submit) {
      submit.disabled = Boolean(state.submitting)
      submit.textContent = submitLabel()
    }
  }

  function invalidSpecs() {
    const type = typeById[state.typeId]
    for (let i = 0; i < type.dimension_labels.length; i++) {
      const value = Number(state.dims[i])
      // 0 is valid where the catalog minimum is 0 (a flat poly mailer).
      const zeroOk =
        Boolean(type.min_dimensions) && type.min_dimensions[i] === 0
      if (!(value > 0 || (zeroOk && value === 0))) {
        return `Enter the ${type.dimension_labels[i].toLowerCase()} as a number ${
          zeroOk ? "of 0 or more" : "above 0"
        }.`
      }
    }
    if (!(Number(state.quantity) > 0)) {
      return "Enter how many pieces you need."
    }
    return ""
  }

  // --------------------------------------------------------------- pricing
  let quoteTimer: any = null
  let quoteSeq = 0
  function requestQuote(delay?: number) {
    clearTimeout(quoteTimer)
    // Responses to earlier requests are stale from here on, even while this
    // one is still debouncing.
    const seq = ++quoteSeq
    const problem = invalidSpecs()
    if (problem) {
      // No price (and no order button) for specs that can't be quoted.
      state.quote = null
      state.error = problem
      state.loading = false
      return renderPrice()
    }
    state.loading = true
    renderPrice()
    quoteTimer = setTimeout(
      async () => {
        try {
          const res = await api("/store/instant-quote/estimate", {
            specs: specs(),
            visitor_id: TRACKING ? visitorId : undefined,
            page: page(),
          })
          if (seq !== quoteSeq) {
            return
          }
          state.quote = res.quote
          state.error = ""
        } catch (error: any) {
          if (seq !== quoteSeq) {
            return
          }
          // The price on screen is for earlier specs; don't offer to order it.
          state.quote = null
          state.error = error.message
        }
        state.loading = false
        renderPrice()
      },
      delay == null ? 250 : delay
    )
  }

  // ---------------------------------------------------------------- events
  // An AI parse still in flight when the buyer orders, closes or reopens is
  // dropped, so it never replaces the specs they chose.
  let parseSeq = 0
  function dropParse() {
    parseSeq++
    state.aiBusy = false
  }

  function applySpecs(s: any) {
    resetForType(s.product_type)
    if (s.dimensions && s.dimensions.length) state.dims = s.dimensions.slice()
    if (s.unit === "cm" || s.unit === "in") state.unit = s.unit
    if (s.quantity) state.quantity = s.quantity
    if (s.material) state.material = s.material
    if (s.print) state.print = s.print
    if (s.finishes) state.finishes = s.finishes.slice()
    if (s.addons) state.addons = s.addons.slice()
    state.rush = Boolean(s.rush)
  }

  function open(options?: any) {
    options = options || {}
    if (options.specs && typeById[options.specs.product_type]) {
      applySpecs(options.specs)
    } else if (
      options.typeId &&
      typeById[options.typeId] &&
      options.typeId !== state.typeId
    ) {
      resetForType(options.typeId)
    }
    lastFocus = doc.activeElement
    dropParse()
    state.open = true
    state.step = "configure"
    state.error = ""
    mount()
    render()
    requestQuote(0)
    track("quote_open", {
      pt: state.typeId,
      d: { source: options.source || "button" },
    })
    const first: any = root.querySelector("#po-qty")
    if (first) first.focus()
  }

  function close() {
    dropParse()
    state.open = false
    render()
    if (lastFocus && lastFocus.focus) {
      lastFocus.focus()
    }
  }

  container.addEventListener("click", async (event: any) => {
    const target = event.target as HTMLElement
    const act =
      target.closest && (target.closest("[data-act]") as HTMLElement | null)
    const action = act ? act.getAttribute("data-act") : null

    if (action === "backdrop" && target === act) {
      return close()
    }
    if (action === "open") {
      return open({ source: "floating_button" })
    }
    if (action === "close") {
      return close()
    }
    if (action === "back") {
      state.step = "configure"
      state.error = ""
      return render()
    }
    if (action === "next") {
      if (!state.quote || state.loading || invalidSpecs()) {
        return
      }
      track("quote_order_click", {
        pt: state.typeId,
        d: { total: state.quote.total, quantity: state.quote.quantity },
      })
      dropParse()
      state.step = "contact"
      state.error = ""
      render()
      const name: any = root.querySelector("#po-name")
      if (name) name.focus()
      return
    }
    if (action === "describe") {
      const box: any = root.querySelector("#po-describe")
      const text = box ? box.value.trim() : ""
      if (text.length < 3) {
        state.aiNote = "Describe the box, size and quantity first."
        return renderForm()
      }
      state.aiBusy = true
      renderForm()
      const seq = ++parseSeq
      try {
        const res = await api("/store/instant-quote/parse", {
          text,
          product_type: state.typeId,
          visitor_id: TRACKING ? visitorId : undefined,
          page: page(),
        })
        if (seq !== parseSeq) {
          return
        }
        // The parsed quote wins over any estimate still debouncing or in flight.
        clearTimeout(quoteTimer)
        quoteSeq++
        applySpecs(res.quote.specs)
        state.quote = res.quote
        state.loading = false
        state.error = ""
        renderPrice()
        state.aiNote =
          (res.missing && res.missing.length
            ? `Please check: ${res.missing.join(", ")}. `
            : "Filled in from your description. ") + (res.notes || "")
      } catch (error: any) {
        if (seq !== parseSeq) {
          return
        }
        state.aiNote = error.message
      }
      state.aiBusy = false
      renderForm()
      return
    }

    const chip =
      target.closest &&
      (target.closest("[data-qty],[data-unit]") as HTMLElement | null)
    if (chip) {
      if (chip.hasAttribute("data-qty")) {
        state.quantity = Number(chip.getAttribute("data-qty"))
      } else {
        const unit = chip.getAttribute("data-unit")
        if (unit !== state.unit) {
          const factor = unit === "cm" ? 2.54 : 1 / 2.54
          state.dims = state.dims.map(
            (value: number) => Math.round(Number(value) * factor * 100) / 100
          )
          state.unit = unit
        }
      }
      renderForm()
      requestQuote(0)
    }
  })

  function renderForm() {
    // Only the configure step has this slot; never fall back to render(),
    // which would wipe a contact form in progress.
    const slot = container.querySelector('[data-slot="form"]')
    if (!slot) {
      return
    }
    const active: any = root.activeElement
    const activeId = active && active.id
    const describe: any = root.querySelector("#po-describe")
    const describeText = describe ? describe.value : ""
    slot.innerHTML = fieldHtml()
    const newDescribe: any = root.querySelector("#po-describe")
    if (newDescribe) newDescribe.value = describeText
    if (activeId) {
      const again: any = root.getElementById(activeId)
      if (again) again.focus()
    }
  }

  container.addEventListener("input", (event: any) => {
    const target = event.target as HTMLInputElement
    if (target.hasAttribute("data-dim")) {
      state.dims[Number(target.getAttribute("data-dim"))] = target.value
      return requestQuote()
    }
    if (target.getAttribute("data-field") === "quantity") {
      state.quantity = target.value
      return requestQuote(400)
    }
  })

  container.addEventListener("change", (event: any) => {
    const target = event.target as HTMLInputElement
    const field = target.getAttribute("data-field")
    if (field === "type") {
      resetForType(target.value)
      state.quote = null
      renderForm()
      return requestQuote(0)
    }
    if (field === "print" || field === "material") {
      state[field] = target.value
      return requestQuote(0)
    }
    if (field === "rush") {
      state.rush = target.checked
      return requestQuote(0)
    }
    const finish = target.getAttribute("data-finish")
    const addon = target.getAttribute("data-addon")
    if (finish || addon) {
      const list = finish ? state.finishes : state.addons
      const id = finish || addon
      const index = list.indexOf(id)
      if (target.checked && index < 0) list.push(id)
      if (!target.checked && index >= 0) list.splice(index, 1)
      return requestQuote(0)
    }
  })

  container.addEventListener("submit", async (event: any) => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    const data = new FormData(form)
    const value = (key: string) => String(data.get(key) || "").trim()
    if (!value("name") || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value("email"))) {
      state.error = "Please enter your name and a valid email."
      render()
      restoreContact(data)
      return
    }
    state.submitting = true
    state.error = ""
    render()
    restoreContact(data)
    try {
      const country = value("country")
      const res = await api("/store/instant-quote/order", {
        specs: specs(),
        contact: {
          name: value("name"),
          email: value("email"),
          company: value("company") || undefined,
          phone: value("phone") || undefined,
          website: value("website") || undefined,
        },
        country_code: country && country !== "xx" ? country : "zz",
        notes: value("notes") || undefined,
        visitor_id: TRACKING ? visitorId : undefined,
        page: page(),
        hp: value("hp") || undefined,
      })
      track("quote_submitted", {
        pt: state.typeId,
        d: { rfq_id: res.rfq_id, checkout: Boolean(res.checkout_url) },
      })
      flush()
      if (res.checkout_url) {
        win.location.assign(res.checkout_url)
        return
      }
      state.result = res
      state.step = "done"
      state.submitting = false
      render()
    } catch (error: any) {
      state.submitting = false
      state.error = error.message
      render()
      restoreContact(data)
    }
  })

  function restoreContact(data: FormData) {
    const form = root.querySelector(
      '[data-form="contact"]'
    ) as HTMLFormElement | null
    if (!form) {
      return
    }
    data.forEach((value, key) => {
      const field: any = form.elements.namedItem(key)
      if (field && typeof value === "string") {
        field.value = value
      }
    })
  }

  root.addEventListener("keydown", (event: any) => {
    if (!state.open) {
      return
    }
    if (event.key === "Escape") {
      return close()
    }
    if (event.key === "Tab") {
      const focusable = Array.from(
        root.querySelectorAll(
          '.dialog button:not([disabled]), .dialog input:not([tabindex="-1"]), .dialog select, .dialog textarea'
        )
      ) as HTMLElement[]
      if (!focusable.length) {
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && root.activeElement === first) {
        last.focus()
        event.preventDefault()
      } else if (!event.shiftKey && root.activeElement === last) {
        first.focus()
        event.preventDefault()
      }
    }
  })

  // Existing "Request a Quote" CTAs open the instant quote instead.
  doc.addEventListener(
    "click",
    (event: any) => {
      if (
        isQuiet() ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey
      ) {
        return
      }
      const el =
        event.target &&
        event.target.closest &&
        event.target.closest("a,[data-packoasis-quote]")
      if (!el || host.contains(el)) {
        return
      }
      if (el.hasAttribute("data-packoasis-quote")) {
        event.preventDefault()
        return open({
          typeId: el.getAttribute("data-product-type") || pageType,
          source: "inline",
        })
      }
      const href = el.getAttribute("href") || ""
      const text = (el.textContent || "").trim()
      if (
        /contact-us|request-quote|#instant-quote/i.test(href) &&
        /quote|pricing|get started|start your project/i.test(text)
      ) {
        event.preventDefault()
        open({ source: "cta" })
      }
    },
    true
  )

  // Static "starting at" teasers injected by the SEO/GEO optimizer.
  function fillTeasers() {
    doc.querySelectorAll("[data-packoasis-teaser]").forEach((el: any) => {
      if (el.getAttribute("data-filled")) {
        return
      }
      const type =
        typeById[el.getAttribute("data-product-type") || pageType || ""]
      if (!type) {
        return
      }
      el.setAttribute("data-filled", "1")
      const button = doc.createElement("button")
      button.type = "button"
      button.setAttribute("data-packoasis-quote", "")
      button.setAttribute("data-product-type", type.id)
      button.textContent = `Get your instant price →`
      button.style.cssText =
        "margin-left:8px;background:#1a7f45;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer"
      el.appendChild(button)
    })
  }

  // The mirrored contact page still carries the old Zoho form (action="#").
  // Submit it to the Medusa RFQ API instead, with any project-drawer draft.
  function wireLegacyForm() {
    const form = doc.querySelector(
      'form[name^="WebToLeads"], form#contact2'
    ) as HTMLFormElement | null
    if (!form || form.getAttribute("data-packoasis-wired")) {
      return
    }
    form.setAttribute("data-packoasis-wired", "1")
    form.setAttribute("action", "#")
    const draftId = params.get("draft_id")
    let feedback = doc.getElementById("packoasis-rfq-feedback")
    if (!feedback) {
      feedback = doc.createElement("div")
      feedback.setAttribute("role", "status")
      form.appendChild(feedback)
    }
    const summary = doc.getElementById("packoasis-draft-summary")
    if (summary && draftId) {
      api(
        `/store/project-drafts/current?draft_id=${encodeURIComponent(draftId)}`
      )
        .then((res: any) => {
          const items = (res.draft && res.draft.items) || []
          if (items.length) {
            summary.style.display = "block"
            summary.innerHTML = `<p><strong>Items from your project:</strong></p><ul>${items
              .map(
                (item: any) =>
                  `<li>${esc(item.title)}${item.notes ? ` (${esc(item.notes)})` : ""}</li>`
              )
              .join("")}</ul>`
          }
        })
        .catch(() => {})
    }

    form.addEventListener(
      "submit",
      async (event: any) => {
        event.preventDefault()
        event.stopImmediatePropagation()
        const get = (name: string) => {
          const field: any = form.elements.namedItem(name)
          return field && typeof field.value === "string"
            ? field.value.trim()
            : ""
        }
        const first = get("First Name")
        const last = get("Last Name")
        const email = get("Email")
        const product = get("LEADCF155")
        const quantity = Number(get("LEADCF52"))
        const timeFrame = get("LEADCF7")
        const description = get("Description")
        const show = (message: string, ok: boolean) => {
          feedback!.textContent = message
          // The mirror stylesheet hides this box unless is-success / is-error is set.
          feedback!.classList.add("packoasis-rfq-feedback")
          feedback!.classList.toggle("is-success", ok)
          feedback!.classList.toggle("is-error", !ok)
          feedback!.setAttribute(
            "style",
            `display:block;margin:12px 0;padding:10px 12px;border-radius:8px;background:${
              ok ? "#e8f6ee" : "#fdecec"
            };color:${ok ? "#12542e" : "#8a1c1c"}`
          )
          feedback!.scrollIntoView({ block: "nearest" })
        }
        if (!first || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || !product) {
          return show(
            "Please add your name, a valid email and the product you need.",
            false
          )
        }
        const button = form.querySelector(
          'button[type="submit"]'
        ) as HTMLButtonElement | null
        if (button) button.disabled = true
        try {
          const res = await api("/store/rfqs", {
            contact_name: `${first} ${last}`.trim(),
            email,
            phone: get("Phone") || undefined,
            company: get("Company") || undefined,
            title: `${product} RFQ`,
            application: product,
            quantity: quantity > 0 ? quantity : undefined,
            notes:
              [
                timeFrame ? `Requested time frame: ${timeFrame}` : "",
                description,
              ]
                .filter(Boolean)
                .join("\n\n") || undefined,
            source: draftId ? "project_draft" : "contact_form",
            draft_id: draftId || undefined,
            visitor_id: TRACKING ? visitorId : undefined,
          })
          track("rfq_submitted", { d: { rfq_id: res.rfq && res.rfq.id } })
          flush()
          // The legacy form has a control with id="reset", which shadows form.reset.
          HTMLFormElement.prototype.reset.call(form)
          show(
            `Thanks! Your request was received (reference ${String(res.rfq.id)
              .slice(-8)
              .toUpperCase()}). A PackOasis specialist will reply within one business day.`,
            true
          )
        } catch (error: any) {
          show(error.message, false)
        }
        if (button) button.disabled = false
      },
      true
    )
  }

  function start() {
    mount()
    render()
    fillTeasers()
    wireLegacyForm()
    let preset: any = null
    const encoded = params.get("po_quote")
    if (encoded) {
      try {
        const binary = win.atob(encoded.replace(/-/g, "+").replace(/_/g, "/"))
        preset = JSON.parse(
          new TextDecoder().decode(
            Uint8Array.from(binary, (c: string) => c.charCodeAt(0))
          )
        )
      } catch (e) {}
    }
    if (preset || win.location.hash === "#instant-quote") {
      open({ specs: preset, source: preset ? "deep_link" : "hash" })
    }
  }

  // Next.js swaps pages without reloading this script, so route state is
  // re-derived per pathname: from popstate here and from the storefront via
  // PackOasisQuote.onRoute() after each client-side navigation.
  let lastPath = win.location.pathname
  let lastHref = win.location.href
  function onRoute() {
    const path = win.location.pathname
    if (path === lastPath) {
      return
    }
    const referrer = lastHref
    lastPath = path
    lastHref = win.location.href
    pageType = typeForPath(path)
    maxScroll = 0
    if (state.open && isQuiet()) {
      close()
    } else if (!state.open) {
      // Like a fresh page load: the configurator starts on this page's type.
      if (pageType && pageType !== state.typeId) {
        resetForType(pageType)
        // An estimate for the previous page's product must not land here.
        clearTimeout(quoteTimer)
        quoteSeq++
        state.quote = null
        state.loading = false
      }
      mount()
      render()
    }
    fillTeasers()
    // Give the app a moment to update document.title.
    setTimeout(() => {
      if (win.location.pathname === path) {
        track("page_view", { r: referrer.slice(0, 500) })
      }
    }, 100)
  }
  win.addEventListener("popstate", onRoute)

  win.PackOasisQuote = {
    open: (options?: any) => open(options),
    close,
    visitorId: () => visitorId,
    onRoute,
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", start)
  } else {
    start()
  }
}

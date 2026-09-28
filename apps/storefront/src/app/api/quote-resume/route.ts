import { NextRequest, NextResponse } from "next/server"

const RFQ_ID = /^[A-Za-z0-9_-]{1,64}$/
const TOKEN = /^[A-Za-z0-9_-]{1,128}$/
const CART_ID = /^cart_[0-9A-Z]{26}$/

const BACKEND_URL = (
  process.env.MEDUSA_BACKEND_URL || "http://localhost:9000"
).replace(/\/$/, "")

/**
 * Asks the backend for a fresh cart for this signed quote link. The cart id
 * comes back only on this server-to-server call and goes straight into this
 * browser's HttpOnly cookie. Any failure counts as no cart.
 */
async function resumeCart(rfq: string, token: string) {
  try {
    const response = await fetch(`${BACKEND_URL}/packoasis/resume-cart`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ rfq, token }),
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    })
    if (!response.ok) {
      return null
    }
    const body = (await response.json()) as {
      cart_id?: unknown
      country_code?: unknown
    }
    if (typeof body?.cart_id !== "string" || !CART_ID.test(body.cart_id)) {
      return null
    }
    const country =
      typeof body.country_code === "string"
        ? body.country_code.toLowerCase()
        : ""
    return {
      cartId: body.cart_id,
      countryCode: /^[a-z]{2}$/.test(country)
        ? country
        : (process.env.NEXT_PUBLIC_DEFAULT_REGION ?? "us").toLowerCase(),
    }
  } catch {
    return null
  }
}

function contactUs(request: NextRequest) {
  return NextResponse.redirect(new URL("/contact-us.html", request.url), 303)
}

/** rfq and token only ever hold [A-Za-z0-9_-] (checked by the callers). */
function confirmPage(rfq: string, token: string) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Complete your PackOasis order</title>
<style>
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#f6f6f4;color:#1f2328}
main{max-width:30rem;margin:12vh auto;padding:2rem;background:#fff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:1.4rem;margin:0 0 .75rem}
p{line-height:1.5;margin:0 0 1rem;color:#4a4f55}
button{font:inherit;font-weight:600;padding:.8rem 1.4rem;border:0;border-radius:8px;background:#1f2328;color:#fff;cursor:pointer}
a{color:inherit}
</style>
</head>
<body>
<main>
<h1>Complete your PackOasis order</h1>
<p>Continue to checkout to order your quote. If it has expired, it is re-priced at today's prices first.</p>
<p>Already checking out this quote on another device? Finish there: continuing here starts a new checkout, and the other one can no longer be completed.</p>
<form method="post" action="/api/quote-resume">
<input type="hidden" name="rfq" value="${rfq}">
<input type="hidden" name="token" value="${token}">
<button type="submit">Continue to checkout</button>
</form>
<p style="margin-top:1.5rem">Questions? <a href="/contact-us.html">Contact us</a>.</p>
</main>
</body>
</html>`
}

/**
 * "Complete my order" link from quote emails. Opening it only shows a
 * confirmation page: mail scanners and link previews fetch links, and every
 * resume mints a fresh cart that supersedes the quote's current one (such as
 * the cart the buyer is already checking out with from the widget). The cart
 * is only made when the buyer presses the button (POST below).
 */
export async function GET(request: NextRequest) {
  const rfq = request.nextUrl.searchParams.get("rfq") ?? ""
  const token = request.nextUrl.searchParams.get("token") ?? ""
  if (!RFQ_ID.test(rfq) || !TOKEN.test(token)) {
    return contactUs(request)
  }
  return new NextResponse(confirmPage(rfq, token), {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "referrer-policy": "no-referrer",
      "x-robots-tag": "noindex",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    },
  })
}

/**
 * The confirmation page's form: the backend mints a fresh cart for the quote
 * (re-priced if it expired), which is attached to this browser before
 * jumping to the address step. A forwarded link only ever gets its opener a
 * new cart, never one somebody else can read. Posts from other sites are
 * sent to the confirmation page instead.
 */
export async function POST(request: NextRequest) {
  let form: FormData | null = null
  try {
    form = await request.formData()
  } catch {
    form = null
  }
  const rfq = String(form?.get("rfq") ?? "")
  const token = String(form?.get("token") ?? "")
  if (!RFQ_ID.test(rfq) || !TOKEN.test(token)) {
    return contactUs(request)
  }

  const site = request.headers.get("sec-fetch-site")
  if (site && site !== "same-origin") {
    const confirm = new URL("/api/quote-resume", request.url)
    confirm.searchParams.set("rfq", rfq)
    confirm.searchParams.set("token", token)
    return NextResponse.redirect(confirm, 303)
  }

  const resumed = await resumeCart(rfq, token)
  if (!resumed) {
    return contactUs(request)
  }

  const response = NextResponse.redirect(
    new URL(`/${resumed.countryCode}/checkout?step=address`, request.url),
    303
  )
  // Lax (not Strict) so the cart is still there when the buyer comes back
  // through a link on another site, such as a later quote email.
  response.cookies.set("_medusa_cart_id", resumed.cartId, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  })
  return response
}

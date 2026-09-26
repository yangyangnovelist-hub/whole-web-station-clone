import { NextRequest, NextResponse } from "next/server"

const CART_ID = /^cart_[0-9A-Z]{26}$/

/**
 * Hand-off from the instant quote widget / quote emails to checkout: the
 * backend already created a cart with the locked quote price, so attach it to
 * this browser and jump straight to the address step.
 */
export async function GET(request: NextRequest) {
  const cartId = request.nextUrl.searchParams.get("cart_id") ?? ""
  const country = (
    request.nextUrl.searchParams.get("country") ??
    process.env.NEXT_PUBLIC_DEFAULT_REGION ??
    "us"
  ).toLowerCase()
  const countryCode = /^[a-z]{2}$/.test(country) ? country : "us"

  if (!CART_ID.test(cartId)) {
    return NextResponse.redirect(new URL("/contact-us.html", request.url), 303)
  }

  const response = NextResponse.redirect(
    new URL(`/${countryCode}/checkout?step=address`, request.url),
    303
  )
  // Lax (not Strict) so the cookie survives the redirect when the buyer
  // arrives from an email link on another site.
  response.cookies.set("_medusa_cart_id", cartId, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
  })
  return response
}

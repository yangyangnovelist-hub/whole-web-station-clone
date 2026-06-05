import { startGoogleCustomerAuth, transferCart } from "@lib/data/customer"
import { NextRequest, NextResponse } from "next/server"

const buildRedirectUrl = (request: NextRequest, path: string) =>
  new URL(path, request.url)

const setAuthCookie = (response: NextResponse, token: string) => {
  response.cookies.set("_medusa_jwt", token, {
    maxAge: 60 * 60 * 24 * 7,
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
  })
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ countryCode: string }> }
) {
  const { countryCode } = await context.params

  try {
    const result = await startGoogleCustomerAuth(countryCode)

    if (typeof result !== "string") {
      return NextResponse.redirect(result.location)
    }

    await transferCart(result)

    const response = NextResponse.redirect(
      buildRedirectUrl(request, `/${countryCode}/account`)
    )
    setAuthCookie(response, result)

    return response
  } catch {
    return NextResponse.redirect(
      buildRedirectUrl(
        request,
        `/${countryCode}/account?auth_error=google_unavailable`
      )
    )
  }
}

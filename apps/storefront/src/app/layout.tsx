import { getBaseURL } from "@lib/util/env"
import PackOasisRouteSync from "@modules/common/components/packoasis-route-sync"
import { Metadata } from "next"
import Script from "next/script"
import "styles/globals.css"

const BACKEND_URL = process.env.MEDUSA_BACKEND_URL || "http://localhost:9000"
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY

export const metadata: Metadata = {
  metadataBase: new URL(getBaseURL()),
}

export default function RootLayout(props: { children: React.ReactNode }) {
  return (
    <html lang="en" data-mode="light">
      <body>
        <main className="relative">{props.children}</main>
        {PUBLISHABLE_KEY && (
          <>
            <Script
              src={`${BACKEND_URL}/packoasis/widget.js`}
              strategy="afterInteractive"
              data-publishable-key={PUBLISHABLE_KEY}
            />
            <PackOasisRouteSync />
          </>
        )}
      </body>
    </html>
  )
}

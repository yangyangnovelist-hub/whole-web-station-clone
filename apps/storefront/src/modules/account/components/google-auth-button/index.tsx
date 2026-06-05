"use client"

import LocalizedClientLink from "@modules/common/components/localized-client-link"

type GoogleAuthButtonProps = {
  copy?: string
}

const GoogleAuthButton = ({
  copy = "Continue with Google",
}: GoogleAuthButtonProps) => {
  return (
    <LocalizedClientLink
      href="/account/google"
      className="flex w-full items-center justify-center gap-3 rounded-full border border-[#d7e5d6] bg-white px-5 py-3 text-sm font-semibold text-[#17303b] transition hover:-translate-y-0.5 hover:border-[#8cc9a8] hover:shadow-[0_16px_40px_rgba(23,48,59,0.08)]"
      data-testid="google-auth-button"
    >
      <span
        className="flex h-9 w-9 items-center justify-center rounded-full border border-[#e3ece2] bg-[#f7faf4]"
        aria-hidden="true"
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4">
          <path
            fill="#4285F4"
            d="M21.6 12.23c0-.72-.06-1.26-.19-1.82H12v3.44h5.53c-.11.85-.72 2.13-2.07 2.99l-.02.12 3 2.27.21.02c1.94-1.75 2.95-4.31 2.95-7.02Z"
          />
          <path
            fill="#34A853"
            d="M12 21.9c2.7 0 4.97-.87 6.63-2.38l-3.16-2.4c-.85.58-1.99.98-3.47.98-2.64 0-4.88-1.75-5.68-4.18l-.12.01-3.12 2.36-.04.11A10.03 10.03 0 0 0 12 21.9Z"
          />
          <path
            fill="#FBBC05"
            d="M6.32 13.92A5.96 5.96 0 0 1 6 12c0-.67.11-1.32.3-1.92l-.01-.13-3.16-2.4-.1.05A9.75 9.75 0 0 0 2 12c0 1.57.38 3.06 1.03 4.4l3.29-2.48Z"
          />
          <path
            fill="#EA4335"
            d="M12 5.9c1.88 0 3.15.8 3.87 1.46l2.82-2.7C16.96 3.1 14.7 2.1 12 2.1A10.03 10.03 0 0 0 3.03 7.6l3.27 2.48C7.12 7.65 9.36 5.9 12 5.9Z"
          />
        </svg>
      </span>
      <span>{copy}</span>
    </LocalizedClientLink>
  )
}

export default GoogleAuthButton

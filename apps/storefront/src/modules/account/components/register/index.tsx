"use client"

import { useActionState } from "react"
import GoogleAuthButton from "@modules/account/components/google-auth-button"
import Input from "@modules/common/components/input"
import { LOGIN_VIEW } from "@modules/account/templates/login-template"
import ErrorMessage from "@modules/checkout/components/error-message"
import { SubmitButton } from "@modules/checkout/components/submit-button"
import LocalizedClientLink from "@modules/common/components/localized-client-link"
import { signup } from "@lib/data/customer"

type Props = {
  setCurrentView: (view: LOGIN_VIEW) => void
}

const Register = ({ setCurrentView }: Props) => {
  const [message, formAction] = useActionState(signup, null)

  return (
    <div className="w-full max-w-[28rem]" data-testid="register-page">
      <div className="mb-8 space-y-3">
        <span className="inline-flex rounded-full border border-[#cde6cf] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.35em] text-[#3ea26f]">
          New Account
        </span>
        <h1 className="font-serif text-4xl leading-[0.98] text-[#11263a]">
          Create your PackOasis workspace.
        </h1>
        <p className="text-base leading-7 text-[#5e6f7f]">
          Save RFQs, keep project drafts attached to your account, and manage
          quote follow-ups from one place.
        </p>
      </div>

      <div className="space-y-4">
        <GoogleAuthButton copy="Sign up with Google" />

        <div className="flex items-center gap-3">
          <span className="h-px flex-1 bg-[#dfe9dd]" />
          <span className="text-[11px] font-semibold uppercase tracking-[0.3em] text-[#8b9aa6]">
            Or continue with email
          </span>
          <span className="h-px flex-1 bg-[#dfe9dd]" />
        </div>
      </div>

      <form className="mt-6 flex w-full flex-col gap-4" action={formAction}>
        <div className="grid grid-cols-1 gap-3 small:grid-cols-2">
          <Input
            label="First name"
            name="first_name"
            required
            autoComplete="given-name"
            data-testid="first-name-input"
          />
          <Input
            label="Last name"
            name="last_name"
            required
            autoComplete="family-name"
            data-testid="last-name-input"
          />
        </div>
        <div className="flex flex-col gap-y-3">
          <Input
            label="Email"
            name="email"
            required
            type="email"
            autoComplete="email"
            data-testid="email-input"
          />
          <Input
            label="Phone"
            name="phone"
            type="tel"
            autoComplete="tel"
            data-testid="phone-input"
          />
          <Input
            label="Password"
            name="password"
            required
            type="password"
            autoComplete="new-password"
            data-testid="password-input"
          />
        </div>
        <ErrorMessage error={message} data-testid="register-error" />
        <span className="text-sm leading-6 text-[#6b7b89]">
          By creating an account, you agree to PackOasis&apos;s{" "}
          <LocalizedClientLink
            href="/content/privacy-policy"
            className="font-semibold underline decoration-[#8bc9a7] underline-offset-4"
          >
            Privacy Policy
          </LocalizedClientLink>{" "}
          and{" "}
          <LocalizedClientLink
            href="/content/terms-of-use"
            className="font-semibold underline decoration-[#8bc9a7] underline-offset-4"
          >
            Terms of Use
          </LocalizedClientLink>
          .
        </span>
        <SubmitButton
          className="h-14 w-full rounded-full bg-[#13273b] text-base font-semibold text-white hover:bg-[#1b3550]"
          data-testid="register-button"
        >
          Create account
        </SubmitButton>
      </form>

      <div className="mt-8 rounded-[28px] border border-[#dbe7d8] bg-[#f6fbf3] px-5 py-4">
        <p className="text-sm leading-6 text-[#5e6f7f]">
          Already have an account connected to RFQs or orders?
        </p>
        <button
          type="button"
          onClick={() => setCurrentView(LOGIN_VIEW.SIGN_IN)}
          className="mt-3 text-sm font-semibold text-[#17303b] underline decoration-[#8bc9a7] underline-offset-4"
        >
          Sign in
        </button>
      </div>
    </div>
  )
}

export default Register

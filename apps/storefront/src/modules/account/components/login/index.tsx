import { login } from "@lib/data/customer"
import GoogleAuthButton from "@modules/account/components/google-auth-button"
import { LOGIN_VIEW } from "@modules/account/templates/login-template"
import ErrorMessage from "@modules/checkout/components/error-message"
import { SubmitButton } from "@modules/checkout/components/submit-button"
import Input from "@modules/common/components/input"
import { useActionState } from "react"

type Props = {
  setCurrentView: (view: LOGIN_VIEW) => void
}

const Login = ({ setCurrentView }: Props) => {
  const [message, formAction] = useActionState(login, null)

  return (
    <div className="w-full max-w-[26rem]" data-testid="login-page">
      <div className="mb-8 space-y-3">
        <span className="inline-flex rounded-full border border-[#cde6cf] px-4 py-2 text-[11px] font-semibold uppercase tracking-[0.35em] text-[#3ea26f]">
          Account Access
        </span>
        <h1 className="font-serif text-4xl leading-[0.98] text-[#11263a]">
          Welcome back to PackOasis.
        </h1>
        <p className="text-base leading-7 text-[#5e6f7f]">
          Track quotes, revisit your packaging drafts, and keep orders moving
          without leaving the storefront.
        </p>
      </div>

      <div className="space-y-4">
        <GoogleAuthButton copy="Continue with Google" />

        <div className="flex items-center gap-3">
          <span className="h-px flex-1 bg-[#dfe9dd]" />
          <span className="text-[11px] font-semibold uppercase tracking-[0.3em] text-[#8b9aa6]">
            Or sign in with email
          </span>
          <span className="h-px flex-1 bg-[#dfe9dd]" />
        </div>
      </div>

      <form className="mt-6 w-full space-y-4" action={formAction}>
        <div className="flex flex-col gap-y-3">
          <Input
            label="Email"
            name="email"
            type="email"
            title="Enter a valid email address."
            autoComplete="email"
            required
            data-testid="email-input"
          />
          <Input
            label="Password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            data-testid="password-input"
          />
        </div>

        <ErrorMessage error={message} data-testid="login-error-message" />

        <SubmitButton
          data-testid="sign-in-button"
          className="mt-2 h-14 w-full rounded-full bg-[#13273b] text-base font-semibold text-white hover:bg-[#1b3550]"
        >
          Sign in
        </SubmitButton>
      </form>

      <div className="mt-8 rounded-[28px] border border-[#dbe7d8] bg-[#f6fbf3] px-5 py-4">
        <p className="text-sm leading-6 text-[#5e6f7f]">
          Prefer to create a new workspace profile for RFQs and production
          updates?
        </p>
        <button
          type="button"
          onClick={() => setCurrentView(LOGIN_VIEW.REGISTER)}
          className="mt-3 text-sm font-semibold text-[#17303b] underline decoration-[#8bc9a7] underline-offset-4"
          data-testid="register-button"
        >
          Create an account
        </button>
      </div>

      <p className="mt-5 text-xs leading-6 text-[#8b9aa6]">
        Google sign-in uses Medusa customer auth and returns you to PackOasis
        when authorization completes.
      </p>
    </div>
  )
}

export default Login

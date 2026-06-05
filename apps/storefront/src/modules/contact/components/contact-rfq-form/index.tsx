"use client"

import { useActionState } from "react"

import LocalizedClientLink from "@modules/common/components/localized-client-link"

import {
  submitContactRfq,
  type ContactRfqFormState,
} from "@modules/contact/actions"

const initialState: ContactRfqFormState = {
  success: false,
  message: null,
  rfqId: null,
}

const productOptions = [
  "Folding Carton Boxes",
  "Corrugated Boxes",
  "Rigid Boxes",
  "Paper Bags",
  "Gusset Bags",
  "Stand-up Pouches",
  "Kraft Pouches",
  "Floor Displays",
  "Cardboard Inserts",
  "Molded Pulp Inserts",
  "Cup Sleeves",
  "Labels & Stickers",
  "Kraft Paper Mailers",
  "Poly Mailers",
  "Recycled Bubble Mailers",
  "Bubble Mailers",
  "Packing Tape",
  "Tissue Paper",
  "Food Grade Tissue Paper",
  "Other",
]

const timeFrameOptions = [
  { value: "", label: "" },
  { value: "less_than_1_month", label: "Less than 1 month" },
  { value: "one_to_three_months", label: "1 - 3 months" },
  { value: "more_than_3_months", label: "More than 3 months" },
]

type ContactRfqFormProps = {
  initialProduct?: string
  initialQuantity?: number
  initialDescription?: string
  hasDraftItems?: boolean
}

export default function ContactRfqForm({
  initialProduct,
  initialQuantity,
  initialDescription,
  hasDraftItems,
}: ContactRfqFormProps) {
  const [state, formAction] = useActionState(submitContactRfq, initialState)

  if (state.success) {
    return (
      <div className="form-body theme-green">
        <div className="spacer-t40 spacer-b30">
          <div className="tagline">
            <span>Request received</span>
          </div>
        </div>
        <div className="section">
          <p className="contact-form-message contact-form-message-success">
            {state.message}
          </p>
          {state.rfqId && (
            <p className="contact-form-reference">
              Reference: <strong>{state.rfqId}</strong>
            </p>
          )}
        </div>
        <div className="form-footer">
          <LocalizedClientLink href="/" className="button">
            Return home
          </LocalizedClientLink>
          <button
            type="button"
            className="button btn-primary"
            onClick={() => window.location.reload()}
          >
            Submit another request
          </button>
        </div>
      </div>
    )
  }

  return (
    <form action={formAction} id="contact2">
      <div className="form-body theme-green">
        <div className="spacer-t40 spacer-b30">
          <div className="tagline">
            <span>Contact Info</span>
          </div>
        </div>

        {hasDraftItems && (
          <div className="section">
            <p className="contact-form-message">
              Items in your current project drawer will be attached to this RFQ
              when you submit the form.
            </p>
          </div>
        )}

        <div className="frm-row">
          <div className="colm colm6">
            <div className="section">
              <label htmlFor="firstname" className="field-label">
                First Name<em>*</em>
              </label>
              <span className="field">
                <input
                  type="text"
                  name="first_name"
                  id="firstname"
                  className="gui-input required"
                  placeholder="John"
                  required
                  data-testid="contact-first-name"
                />
              </span>
            </div>
          </div>
          <div className="colm colm6">
            <div className="section">
              <label htmlFor="lastname" className="field-label">
                Last Name<em>*</em>
              </label>
              <span className="field">
                <input
                  type="text"
                  name="last_name"
                  id="lastname"
                  className="gui-input required"
                  placeholder="Doe"
                  required
                  data-testid="contact-last-name"
                />
              </span>
            </div>
          </div>
        </div>

        <div className="frm-row">
          <div className="colm colm6">
            <div className="section">
              <label htmlFor="email" className="field-label">
                Email<em>*</em>
              </label>
              <span className="field">
                <input
                  type="email"
                  name="email"
                  id="email"
                  className="gui-input required"
                  placeholder="hello@packoasis.com"
                  required
                  data-testid="contact-email"
                />
              </span>
            </div>
          </div>
          <div className="colm colm6">
            <div className="section">
              <label htmlFor="phone" className="field-label">
                Phone Number<em>*</em>
              </label>
              <span className="field">
                <input
                  type="tel"
                  name="phone"
                  id="phone"
                  className="gui-input required"
                  placeholder="(888) 622-2819"
                  required
                  data-testid="contact-phone"
                />
              </span>
            </div>
          </div>
        </div>

        <div className="section">
          <label htmlFor="company" className="field-label">
            Company name
          </label>
          <span className="field">
            <input
              type="text"
              name="company"
              id="company"
              className="gui-input"
              placeholder="Company"
              data-testid="contact-company"
            />
          </span>
        </div>

        <div className="spacer-t40 spacer-b30">
          <div className="tagline">
            <span>Project Info</span>
          </div>
        </div>

        <div className="section">
          <label htmlFor="product" className="field-label">
            Product<em>*</em>
          </label>
          <label className="field select">
            <select
              id="product"
              name="product"
              defaultValue={initialProduct || ""}
              required
              data-testid="contact-product"
            >
              <option value="">Select a product</option>
              {productOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <i className="arrow" />
          </label>
        </div>

        <div className="frm-row">
          <div className="section colm colm6">
            <label htmlFor="quantity" className="field-label">
              Quantity<em>*</em>
            </label>
            <span className="field">
              <input
                type="number"
                name="quantity"
                id="quantity"
                min={1}
                defaultValue={initialQuantity ?? ""}
                className="gui-input"
                required
                data-testid="contact-quantity"
              />
              <i className="arrow" />
            </span>
          </div>
          <div className="section colm colm6">
            <label htmlFor="time_frame" className="field-label">
              Time frame
            </label>
            <label className="field select">
              <select id="time_frame" name="time_frame" data-testid="contact-time-frame">
                {timeFrameOptions.map((option) => (
                  <option key={option.value || "empty"} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <i className="arrow" />
            </label>
          </div>
        </div>

        <div className="section">
          <label htmlFor="project_description" className="field-label">
            Please describe your project.<em>*</em>
          </label>
          <label className="field prepend-icon">
            <textarea
              className="gui-textarea"
              name="project_description"
              id="project_description"
              defaultValue={initialDescription || ""}
              placeholder="Please provide details on the box style you are looking for, the dimensions, and whether you want printing inside, outside, or both. Additionally, feel free to share any other details or specific requirements that will help us better understand your project."
              required
              data-testid="contact-description"
            />
            <span className="field-icon">
              <i className="fa fa-file-text" />
            </span>
          </label>
        </div>

        {state.message && (
          <div className="section">
            <p className="contact-form-message contact-form-message-error">
              {state.message}
            </p>
          </div>
        )}
      </div>

      <div className="form-footer">
        <p>
          By submitting this form you agree to our{" "}
          <LocalizedClientLink href="/content/terms-of-use">
            Terms of Service
          </LocalizedClientLink>{" "}
          and{" "}
          <LocalizedClientLink href="/content/privacy-policy">
            Privacy Policy
          </LocalizedClientLink>
          .
        </p>

        <button type="reset" className="button" id="reset">
          Reset
        </button>
        <button
          type="submit"
          className="button btn-primary"
          data-testid="contact-form-submit"
        >
          Submit
        </button>
      </div>
    </form>
  )
}

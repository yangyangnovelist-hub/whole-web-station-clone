import { z } from "zod"

export const VisitorIdSchema = z
  .string()
  .regex(/^[A-Za-z0-9_-]{8,64}$/, "Invalid visitor id")

export const SpecsSchema = z.object({
  product_type: z.string().min(1, "product_type is required"),
  dimensions: z.array(z.coerce.number().positive()).max(3).optional(),
  unit: z.enum(["in", "cm", "mm"]).optional(),
  quantity: z.coerce.number().int().positive().optional(),
  material: z.string().max(64).optional(),
  print: z.string().max(32).optional(),
  finishes: z.array(z.string().max(32)).max(8).optional(),
  addons: z.array(z.string().max(32)).max(4).optional(),
  rush: z.boolean().optional(),
})

export const PageSchema = z
  .object({
    url: z.string().max(500).optional(),
    path: z.string().max(300).optional(),
    title: z.string().max(200).optional(),
  })
  .optional()

export function validationError(error: z.ZodError) {
  return {
    code: "VALIDATION_ERROR",
    message: error.errors
      .map((issue) => `${issue.path.join(".") || "body"}: ${issue.message}`)
      .join(", "),
  }
}

import { 
  MedusaError
} from "@medusajs/framework/utils"
import { 
  IProductModuleService,
} from "@medusajs/framework/types"
import { Modules } from "@medusajs/framework/utils"

export default async function populatePackOasisProducts({ container }) {
  const productModuleService: IProductModuleService = container.resolve(Modules.PRODUCT)

  console.log("Fetching Categories for Product Association...")
  const categories = await productModuleService.listProductCategories({
    handle: ["custom-mailers", "folding-carton-boxes"]
  })

  const categoryMap = new Map(categories.map(c => [c.handle, c.id]))

  const productsToCreate = [
    {
      title: "Custom Printed Mailers",
      handle: "custom-printed-mailers",
      description: "Ship in style with custom mailers, fully customizable to any size and option. Our custom shipping mailers can be 100% recycable and compostable.",
      thumbnail: "/media.packoasis.com/media_upload/coding_guide/mega-menu/bag-3.jpg",
      categories: categoryMap.has("custom-mailers") ? [{ id: categoryMap.get("custom-mailers") }] : [],
      options: [
        { title: "Size", values: ["Small", "Medium", "Large"] },
        { title: "Material", values: ["Recycled Paper", "Compostable Poly"] }
      ],
      variants: [
        { 
          title: "Small / Recycled", 
          sku: "MAIL-S-REC", 
          inventory_quantity: 1000,
          options: { "Size": "Small", "Material": "Recycled Paper" }
        },
        { 
          title: "Medium / Recycled", 
          sku: "MAIL-M-REC", 
          inventory_quantity: 1000,
          options: { "Size": "Medium", "Material": "Recycled Paper" }
        },
        { 
          title: "Large / Recycled", 
          sku: "MAIL-L-REC", 
          inventory_quantity: 1000,
          options: { "Size": "Large", "Material": "Recycled Paper" }
        }
      ],
      status: "published"
    },
    {
      title: "Folding Carton Boxes",
      handle: "custom-folding-carton-boxes",
      description: "Versatile and high-quality folding carton boxes for retail and display. Perfectly suited for cosmetics, pharmaceuticals, and consumer goods.",
      thumbnail: "/media.packoasis.com/media_upload/coding_guide/categories/folding-carton.jpg",
      categories: categoryMap.has("folding-carton-boxes") ? [{ id: categoryMap.get("folding-carton-boxes") }] : [],
      options: [
        { title: "Finish", values: ["Matte", "Glossy", "Soft Touch"] }
      ],
      variants: [
        { 
          title: "Matte Finish", 
          sku: "FOLD-MATTE", 
          inventory_quantity: 500,
          options: { "Finish": "Matte" }
        },
        { 
          title: "Glossy Finish", 
          sku: "FOLD-GLOSS", 
          inventory_quantity: 500,
          options: { "Finish": "Glossy" }
        }
      ],
      status: "published"
    }
  ]

  console.log(`Creating ${productsToCreate.length} Product Prototypes...`)

  for (const productData of productsToCreate) {
    try {
      const existing = await productModuleService.listProducts({ handle: productData.handle })
      if (existing.length > 0) {
        console.log(`Product ${productData.handle} already exists. Skipping...`)
        continue
      }

      await productModuleService.createProducts([productData])
      console.log(`Created Product: ${productData.title}`)
    } catch (error) {
      console.error(`Failed to create product ${productData.handle}:`, error.message)
    }
  }

  console.log("PackOasis Product Prototypes Created Successfully.")
}

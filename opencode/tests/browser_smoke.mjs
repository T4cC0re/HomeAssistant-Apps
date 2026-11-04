import assert from "node:assert/strict"
import { pathToFileURL } from "node:url"

const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href)
const prefix = "/api/hassio_ingress/test-token"
const origin = process.env.OPENCODE_SMOKE_ORIGIN
const browser = await chromium.launch({
  headless: true,
  ...(process.env.OPENCODE_SMOKE_BROWSER ? { executablePath: process.env.OPENCODE_SMOKE_BROWSER } : {}),
})

try {
  const context = await browser.newContext({
    extraHTTPHeaders: {
      "X-Remote-User-Name": "Smoke User",
      "X-Ingress-Path": prefix,
      "X-Forwarded-Proto": "http",
      "X-Forwarded-Host": new URL(origin).host,
    },
  })
  await context.addCookies([{ name: "ingress_session", value: "smoke-session", url: origin }])
  const page = await context.newPage()
  const errors = []
  const failures = []
  const requests = []
  page.on("pageerror", (error) => errors.push(error.message))
  page.on("request", (request) => {
    if (new URL(request.url()).origin === origin) requests.push(new URL(request.url()).pathname)
  })
  page.on("response", (response) => {
    if (response.status() >= 400) failures.push({ url: response.url(), status: response.status() })
  })
  await page.goto(origin + prefix + "/", { waitUntil: "domcontentloaded" })
  await page.getByText("Nothing here yet", { exact: true }).waitFor()
  assert.equal(errors.length, 0, JSON.stringify(errors))
  const slug = Buffer.from(process.env.OPENCODE_SMOKE_DIRECTORY).toString("base64url")
  await page.goto(origin + prefix + "/" + slug + "/session", { waitUntil: "domcontentloaded" })
  await page.getByText("New session", { exact: true }).first().waitFor()
  await page.waitForTimeout(1000)
  assert(page.url().startsWith(origin + prefix + "/new-session?draftId="), page.url())
  assert.equal(errors.length, 0, JSON.stringify(errors))
  assert.equal(failures.length, 0, JSON.stringify(failures))
  assert(requests.every((path) => path === prefix || path.startsWith(prefix + "/")), JSON.stringify(requests))
  assert(requests.some((path) => path === prefix + "/global/event"), "The event stream did not start")
  console.log("The browser loaded the web interface and session route through ingress.")
} finally {
  await browser.close()
}

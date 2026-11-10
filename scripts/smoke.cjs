const assert = require("node:assert/strict");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const crypto = require("node:crypto");
const { chromium } = require(process.env.PW_MODULE || "playwright");

const root = path.resolve(__dirname, "..");
const privateDir = path.join(root, ".secrets");
const evidence = path.join(root, "evidence");
const ca = fs.readFileSync(path.join(privateDir, "ca_cert"));
const cert = new crypto.X509Certificate(fs.readFileSync(path.join(privateDir, "tls_cert")));
const pin = crypto.createHash("sha256").update(cert.publicKey.export({type:"spki",format:"der"})).digest("base64");
const iam = "https://iam.test:8443";
const portal = "https://portal.test:8443";
const realm = "/admin/realms/iam";
const target = process.env.IAM_TARGET || "127.0.0.1";
const capture = process.argv.includes("--capture");
const check = (message) => console.log("PASS " + message);
const pause = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function request(route, {method="GET", token, body, form=false, hostname="iam.test"}={}) {
  return new Promise((resolve, reject) => {
    const data = body && (form ? new URLSearchParams(body).toString() : JSON.stringify(body));
    const headers = {Host:hostname+":8443"};
    if (token) headers.Authorization = "Bearer " + token;
    if (data) {
      headers["Content-Type"] = form ? "application/x-www-form-urlencoded" : "application/json";
      headers["Content-Length"] = Buffer.byteLength(data);
    }
    const req = https.request({host:target, port:8443, servername:hostname, ca, path:route, method, headers}, res => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", chunk => {text += chunk;});
      res.on("end", () => resolve({status:res.statusCode, headers:res.headers, text}));
    });
    req.setTimeout(5000, () => req.destroy(new Error("Request timed out")));
    req.on("error", reject);
    req.end(data);
  });
}

async function waitForIssuer() {
  for (let i=0; i<75; i++) {
    try {
      const r = await request("/realms/iam/.well-known/openid-configuration");
      if (r.status === 200) {
        assert.equal(JSON.parse(r.text).issuer, iam + "/realms/iam");
        if ((await request("/ping", {hostname:"portal.test"})).status === 200) return;
      }
    } catch (err) {
      if (err.code === "ERR_ASSERTION") throw err;
    }
    await pause(4000);
  }
  throw new Error("OIDC discovery did not become ready");
}

async function login(browser, username, password, inspect) {
  const ctx = await browser.newContext({viewport:{width:1280,height:800}});
  try {
    const page = await ctx.newPage();
    await page.goto(portal, {waitUntil:"domcontentloaded", timeout:60000});
    await page.locator("#username").fill(username);
    await page.locator("#password").fill(password);
    const [result] = await Promise.all([
      page.waitForNavigation({waitUntil:"domcontentloaded"}),
      page.locator("#kc-login").click()
    ]);
    await inspect(page, result);
  } finally {
    await ctx.close();
  }
}

async function screenshot(page, name) {
  if (capture) {
    fs.mkdirSync(evidence, {recursive:true});
    await page.screenshot({path:path.join(evidence,name + ".png")});
  }
}

async function main() {
  await waitForIssuer();
  check("01 HTTPS discovery and canonical issuer");
  const adminPassword = fs.readFileSync(path.join(privateDir,"admin_password"),"utf8");
  const r = await request("/realms/master/protocol/openid-connect/token", {
    method:"POST", form:true,
    body:{client_id:"admin-cli", grant_type:"password", username:"lab-admin", password:adminPassword}
  });
  assert.equal(r.status, 200, "bootstrap admin authentication");
  const token = JSON.parse(r.text).access_token;
  async function api(suffix, method, body, status) {
    const result = await request(realm + suffix, {method, token, body});
    assert.equal(result.status, status, method + " " + suffix + ": " + result.text.slice(0,250));
    return result;
  }
  const groups = JSON.parse((await api("/groups?search=portal-users&exact=true","GET",null,200)).text);
  const group = groups.find(g => g.path === "/portal-users");
  assert.ok(group);
  const username = "check-" + crypto.randomBytes(4).toString("hex");
  const password = "Lab!" + crypto.randomBytes(14).toString("hex");
  const nextPassword = "New!" + crypto.randomBytes(14).toString("hex");
  const created = await api("/users","POST",{
    username, enabled:true, email:username+"@lab.example", emailVerified:true,
    firstName:"Lab", lastName:"User", credentials:[{type:"password",value:password,temporary:false}]
  },201);
  const user = "/users/" + created.headers.location.split("/").at(-1);
  check("02 user created via Admin REST API");
  let browser;
  try {
    browser = await chromium.launch({
      executablePath:process.env.BROWSER_PATH || undefined,
      headless:true,
      args:["--no-proxy-server","--host-resolver-rules=MAP iam.test " + target + ", MAP portal.test " + target,
            "--ignore-certificate-errors-spki-list=" + pin]
    });
    await login(browser,username,password,async (page,result) => {
      assert.equal(result.status(),403);
      assert.equal(new URL(page.url()).hostname,"portal.test");
      await screenshot(page,"forbidden");
    });
    check("03 authenticated user without group receives HTTP 403");
    await api(user+"/groups/"+group.id,"PUT",null,204);
    async function allowed(page,result) {
      assert.equal(result.status(),200);
      assert.ok((await page.locator("h1").innerText()).includes("Доступ к порталу разрешён"));
    }
    await login(browser,username,password,async (page,result) => {
      await allowed(page,result);
      await screenshot(page,"allowed");
    });
    check("04 group membership grants access to the portal");
    await api(user+"/reset-password","PUT",{type:"password",value:nextPassword,temporary:false},204);
    async function denied(page) {
      assert.equal(new URL(page.url()).hostname,"iam.test");
      assert.match(await page.locator("body").innerText(), /invalid username or password|invalid credentials|account is disabled|неверн|заблокирован/i);
    }
    await login(browser,username,password,denied);
    await login(browser,username,nextPassword,allowed);
    check("05 reset rejects the old password and accepts the new one");
    await api(user,"PUT",{enabled:false},204);
    await api(user+"/logout","POST",null,204);
    await login(browser,username,nextPassword,async page => {
      await denied(page);
      await screenshot(page,"disabled");
    });
    check("06 disabled account cannot start a new session");
    await api(user,"PUT",{enabled:true},204);
    await login(browser,username,nextPassword,allowed);
    check("07 re-enabled account can sign in again");
  } finally {
    if (browser) await browser.close();
    await api(user,"DELETE",null,204);
  }
  check("08 test account deleted");
}
main().catch(error => {console.error(error); process.exitCode=1;});

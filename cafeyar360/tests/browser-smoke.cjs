const fs = require('node:fs');
fs.mkdirSync('test-artifacts',{recursive:true});
const { chromium } = require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH});
 const context=await browser.newContext();
 const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8010/menu?table=3');
 await page.waitForSelector('.product-card');
 await page.locator('[data-action="add"]').first().click();
 await page.locator('[data-action="open-cart"]').click();
 await page.locator('form[data-form="order"] button[type="submit"]').click();
 await page.waitForSelector('.success-box');
 await page.screenshot({path:'test-artifacts/sprint1-order.png',fullPage:true});
 await page.goto('http://127.0.0.1:8010/staff');
 await page.waitForSelector('#login-dialog[open]');
 await page.locator('[data-login="barista"]').click();
 await page.locator('#login-form button[type="submit"]').click();
 await page.waitForSelector('.order-card');
 const cards=await page.locator('.order-card').count();
 // Customer order was created last; active queue sorts oldest first.
 const card=page.locator('.order-card').last();
 for(const status of ['accepted','preparing','ready','completed']) {
   const response=page.waitForResponse(r=>r.url().endsWith('/status')&&r.request().method()==='PATCH');
   await page.locator('.order-card').last().locator(`[data-status="${status}"]`).click();
   const res=await response;if(res.status()!==200)throw Error('status '+res.status());
   if(status!=='completed')await page.waitForSelector(`.order-card:last-child [data-status="${({accepted:'preparing',preparing:'ready',ready:'completed'})[status]}"]`);
 }
 await context.close();
 const cashier=await browser.newContext();const cash=await cashier.newPage();cash.on('pageerror',e=>errors.push(e.message));
 await cash.goto('http://127.0.0.1:8010/cashier');await cash.waitForSelector('#login-dialog[open]');
 await cash.locator('[data-login="cashier"]').click();await cash.locator('#login-form button[type="submit"]').click();
 await cash.waitForSelector('[data-action="pay"]');
 cash.on('dialog',async d=>{if(d.type()==='prompt')await d.accept(d.message().includes('روش')?'cash':'BROWSER-TEST');else await d.accept()});
 const payment=cash.waitForResponse(r=>r.url().endsWith('/pay')&&r.request().method()==='POST');
 await cash.locator('[data-action="pay"]').last().click();if((await payment).status()!==200)throw Error('payment failed');
 await cash.screenshot({path:'test-artifacts/sprint1-cashier.png',fullPage:true});
 const mobile=await browser.newContext({viewport:{width:390,height:844},isMobile:true});const m=await mobile.newPage();
 await m.goto('http://127.0.0.1:8010/menu?table=3');await m.waitForSelector('.product-card');
 const overflow=await m.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
 await m.screenshot({path:'test-artifacts/sprint1-mobile.png',fullPage:true});
 console.log(JSON.stringify({customerOrder:true,staffCards:cards,statusFlow:true,manualCashPayment:true,mobileHorizontalOverflow:overflow,pageErrors:errors}));
 await browser.close();if(errors.length||overflow)process.exitCode=1;
})().catch(e=>{console.error(e.message);process.exit(1)});

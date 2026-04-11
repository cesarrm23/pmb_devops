/**
 * Playwright E2E: Test all 3 roles — Admin, Developer, Viewer
 */
import { chromium } from 'playwright';

const BASE = 'http://localhost:8075';
const SPA = `${BASE}/web#action=780`;
const USERS = {
    admin:     { login: 'cesarrm23@gmail.com', password: 'cincinati930621', role: 'Administrator' },
    developer: { login: 'devtest@pmb.test',    password: 'devtest123',      role: 'Developer' },
    viewer:    { login: 'viewer@pmb.test',      password: 'viewer123',       role: 'Viewer' },
};

let browser;
const results = [];

function log(test, ok, detail = '') {
    console.log(`[${ok ? 'PASS' : 'FAIL'}] ${test}${detail ? ': ' + detail : ''}`);
    results.push({ test, ok, detail });
}

async function newPage(creds) {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text().substring(0, 80)); });
    await page.request.post(`${BASE}/web/session/authenticate`, {
        data: { jsonrpc: '2.0', method: 'call', params: { db: 'odooal', ...creds } },
    });
    page._jsErrors = errors;
    return page;
}

async function goToAI(page) {
    await page.goto(SPA, { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(5000);
    // Select production instance
    const prod = page.getByText('production', { exact: true }).first();
    if (await prod.count() > 0) {
        await prod.click();
        await page.waitForTimeout(2000);
    }
    // Click AI tab
    const aiTab = page.locator('text=✨ AI').first();
    if (await aiTab.count() > 0) {
        await aiTab.click();
        await page.waitForTimeout(4000);
        return true;
    }
    return false;
}

async function testRole(key) {
    const creds = USERS[key];
    console.log(`\n=== ${creds.role.toUpperCase()} (${creds.login}) ===`);
    const page = await newPage(creds);

    // 1. Can access the SPA?
    await page.goto(SPA, { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(5000);
    const initText = await page.locator('body').innerText();
    const hasOwlError = initText.includes('¡Vaya!');
    const seesDevOps = initText.includes('PMB DevOps') || initText.includes('Branches');
    log(`${creds.role}: SPA loads without OWL error`, !hasOwlError, hasOwlError ? 'OWL error on page' : '');
    log(`${creds.role}: sees DevOps SPA`, seesDevOps);

    // 2. Can see instances?
    const seesProduction = initText.includes('Production') || initText.includes('production');
    log(`${creds.role}: sees instances`, seesProduction);

    if (!seesProduction) {
        await page.screenshot({ path: `/tmp/pmb_role_${key}.png` });
        await page.close();
        return;
    }

    // 3. Select instance and go to AI tab
    const aiOk = await goToAI(page);
    log(`${creds.role}: can access AI tab`, aiOk);

    if (!aiOk) {
        await page.screenshot({ path: `/tmp/pmb_role_${key}.png` });
        await page.close();
        return;
    }

    const aiText = await page.locator('body').innerText();
    await page.screenshot({ path: `/tmp/pmb_role_${key}_ai.png` });

    // 4. Git Changes panel visible?
    const hasGitChanges = aiText.includes('Git Changes');
    log(`${creds.role}: Git Changes panel visible`, hasGitChanges);

    // 5. Auth form behavior
    const hasAuthForm = aiText.includes('Autenticación para commit/push');

    if (key === 'admin') {
        log(`${creds.role}: NO auth form (admin bypass)`, !hasAuthForm);
        const hasCommit = aiText.includes('Commit');
        log(`${creds.role}: commit buttons visible directly`, hasCommit || aiText.includes('No hay cambios'));
    } else {
        log(`${creds.role}: auth form shown`, hasAuthForm);
        const hasCommitBefore = aiText.includes('Commit + Push');
        log(`${creds.role}: commit buttons hidden before auth`, !hasCommitBefore);

        // 6. Try login with wrong password
        const loginField = page.locator('input[placeholder="Usuario (login)"]');
        const pwField = page.locator('input[placeholder="Contraseña"]');
        if (await loginField.count() > 0) {
            await loginField.fill(creds.login);
            await pwField.fill('wrongpassword');
            await page.locator('button:has-text("Iniciar sesión")').click();
            await page.waitForTimeout(2000);
            const errText = await page.locator('body').innerText();
            log(`${creds.role}: wrong password shows error`, errText.includes('incorrecta') || errText.includes('no encontrado'));

            // 7. Login with correct password
            await pwField.fill(creds.password);
            await page.locator('button:has-text("Iniciar sesión")').click();
            await page.waitForTimeout(2000);
            const afterAuth = await page.locator('body').innerText();
            const authGone = !afterAuth.includes('Autenticación para commit/push');
            log(`${creds.role}: auth form disappears after login`, authGone);
            await page.screenshot({ path: `/tmp/pmb_role_${key}_authed.png` });
        }
    }

    // 8. Backend guard test
    const commitResp = await page.request.post(`${BASE}/devops/git/commit`, {
        data: { jsonrpc: '2.0', method: 'call', params: { project_id: 1, repo_path: '/tmp', message: 'test' } },
    });
    const commitResult = (await commitResp.json()).result || {};

    if (key === 'admin') {
        // Admin should NOT get auth_required (might get other error since /tmp isn't a repo)
        log(`${creds.role}: backend allows commit (no auth_required)`, !commitResult.auth_required);
    } else if (key === 'developer') {
        // Developer already authenticated via the form above, should be allowed now
        log(`${creds.role}: backend allows commit after auth`, !commitResult.auth_required,
            commitResult.auth_required ? 'still blocked' : 'allowed (may fail for other reasons)');
    } else {
        // Viewer — test with a fresh session (no git auth)
        const freshPage = await newPage(creds);
        const viewerResp = await freshPage.request.post(`${BASE}/devops/git/commit`, {
            data: { jsonrpc: '2.0', method: 'call', params: { project_id: 1, repo_path: '/tmp', message: 'test' } },
        });
        const viewerResult = (await viewerResp.json()).result || {};
        log(`${creds.role}: backend blocks commit without auth`, viewerResult.auth_required === true);
        await freshPage.close();
    }

    // 9. Check HISTORY tab too
    await page.locator('text=📋 HISTORY').first().click();
    await page.waitForTimeout(3000);
    const histText = await page.locator('body').innerText();
    const seesRepos = histText.includes('odoo') || histText.includes('pmb_devops');
    log(`${creds.role}: can see repos in HISTORY`, seesRepos);

    await page.close();
}

async function main() {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    try {
        await testRole('admin');
        await testRole('developer');
        await testRole('viewer');
    } catch (e) {
        console.error('FATAL:', e.message);
    } finally {
        console.log('\n========== SUMMARY ==========');
        const pass = results.filter(r => r.ok).length;
        const fail = results.filter(r => !r.ok).length;
        console.log(`Total: ${results.length} | PASS: ${pass} | FAIL: ${fail}`);
        if (fail > 0) {
            console.log('\nFailed:');
            results.filter(r => !r.ok).forEach(r =>
                console.log(`  [FAIL] ${r.test}${r.detail ? ': ' + r.detail : ''}`)
            );
        }
        await browser.close();
        process.exit(fail > 0 ? 1 : 0);
    }
}

main();

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {httpSessionConfig,readHttpSessionConfig} from '../codex-config.mjs';
import {runAgent} from '../runner.mjs';

test('custom provider receives HTTP and bounded retries without copying credentials or changing model',()=>{
  const host={model:'existing-model',model_provider:'company',model_providers:{company:{
    name:'Existing',base_url:'https://private.example',env_key:'EXISTING_KEY',
    experimental_bearer_token:'fixture-secret',supports_websockets:true}}};
  const before=JSON.stringify(host),config=httpSessionConfig(host,45000);
  assert.equal(JSON.stringify(host),before);
  assert.equal(config['model_providers.company.supports_websockets'],false);
  assert.equal(config['model_providers.company.request_max_retries'],0);
  assert.equal(config['model_providers.company.stream_max_retries'],0);
  assert.equal(config['model_providers.company.stream_idle_timeout_ms'],20000);
  assert.equal(config.model,undefined);assert.equal(config.model_provider,undefined);
  assert.doesNotMatch(JSON.stringify(config),/fixture-secret|EXISTING_KEY|private\.example/);
  assert.equal(config.sandbox_mode,'read-only');assert.equal(config.features.shell_tool,false);
});

test('native OpenAI provider uses session alias instead of redefining a reserved provider table',()=>{
  const config=httpSessionConfig({},10000),provider=config.model_providers[config.model_provider];
  assert.equal(config.model_providers.openai,undefined);
  assert.equal(provider.requires_openai_auth,true);
  assert.equal(provider.supports_websockets,false);
  assert.equal(provider.stream_idle_timeout_ms,5000);
});

test('reading TOML leaves user config unchanged; missing config works and invalid config fails closed',async t=>{
  const home=await mkdtemp(path.join(os.tmpdir(),'xiaoman-config-test-'));
  t.after(()=>rm(home,{recursive:true,force:true}));
  assert.equal((await readHttpSessionConfig(home,45000)).sandbox_mode,'read-only');
  const file=path.join(home,'config.toml');
  const text='model = "existing-model"\nmodel_provider = "company" # current\n[model_providers.company]\nname="Company"\n';
  await writeFile(file,text);
  assert.equal((await readHttpSessionConfig(home,45000))['model_providers.company.supports_websockets'],false);
  assert.equal(await readFile(file,'utf8'),text);
  await writeFile(file,'model_provider = [invalid');
  await assert.rejects(readHttpSessionConfig(home,45000),/invalid-provider-config/);
});

test('invalid time budgets fail before launching an agent',async()=>{
  for(const timeoutMs of [0,-1,NaN,Infinity,120001]) {
    await assert.rejects(runAgent({directory:'unused',content:'fixture',timeoutMs}),/invalid-timeout/);
  }
});

test('outer wall-clock deadline stops an unresponsive ACP child without another attempt',async t=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'xiaoman-timeout-test-'));
  t.after(()=>rm(directory,{recursive:true,force:true}));
  const start=performance.now();let diagnostics=0;
  await assert.rejects(runAgent({directory,content:'超时探针',timeoutMs:1000,
    onDiagnostic:()=>{diagnostics++;}}),/agent-timeout/);
  assert.ok(performance.now()-start<4000);
  assert.equal(diagnostics,1);
});

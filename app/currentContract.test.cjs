const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {probeSession, DEFAULT_API_BASE_URL} = require('./apiClient.cjs');
const {createRecordsApi, createTextRecord} = require('./records.cjs');
const {createAuthSession} = require('./authSession.cjs');
const baseUrl = DEFAULT_API_BASE_URL;
test('demo default is 8002; session probe distinguishes reachable 401 from transport failure', async () => {
  assert.equal(baseUrl, 'http://127.0.0.1:8002/api/v1');
  for (const status of [200,401]) {
    const result = await probeSession({baseUrl, currentBaseUrl:baseUrl, getAccessToken:()=> 'fixture-token', fetchImpl:async(url, options)=>{
      assert.equal(url, baseUrl+'/auth/me'); assert.equal(options.method,'GET');
      assert.equal(options.headers.Authorization,'Bearer fixture-token');
      return new Response(JSON.stringify(status===200?{user_id:'user-1'}:{detail:'unauthorized'}),{status});
    }});
    assert.deepEqual(result,{reachable:true,authenticated:status===200});
  }
  await assert.rejects(probeSession({baseUrl,currentBaseUrl:baseUrl,fetchImpl:async()=>{throw new Error('network offline');}}),/network offline/);
});
test('server settings probe never sends existing credentials to another target', async()=>{
  await probeSession({baseUrl:'http://other.invalid/api/v1',currentBaseUrl:baseUrl,getAccessToken:()=> 'fixture-token',fetchImpl:async(url,opts)=>{
    assert.equal(opts.headers.Authorization,undefined);return new Response('{}',{status:401});
  }});
});
test('logout clears all local auth and never calls DELETE',async()=>{
  const removed=[];const tokens=[];
  const session=createAuthSession({client:new Proxy({}, {get(){throw new Error('logout must not use HTTP');}}), storage:{multiRemove:async keys=>removed.push(...keys)},setAccessToken:t=>tokens.push(t),mapUser:x=>x});
  await session.clear();assert.deepEqual(tokens,['']);assert.deepEqual(removed,['ambient.accessToken','ambient.currentUser','ambient.user']);
});
test('record list/read/create use canonical DTO and idempotency; text publishes without automatic human approval',async()=>{
  const calls=[];const records=createRecordsApi({get:async(path)=>{calls.push(path);return path.includes('/families/')?{items:[{record_id:'r'}]}:{record_id:'r'};},post:async(path,opts)=>{calls.push([path,opts]);return {record_id:'r'};}});
  assert.deepEqual(await records.list('room'),[{record_id:'r'}]);await records.get('r');
  const payload={room_id:'room',type:'text',title:'memo',description:'Written source',visibility:'FAMILY'};
  const published=await createTextRecord({records,uploads:{publishText:async id=>{assert.equal(id,'r');return {upload_id:id,has_file:true,file_id:'f'};}},payload,idempotencyKey:'fixture-request'});
  assert.equal(published.file_id,'f');assert.equal(calls[0],'/families/room/records');assert.equal(calls[1],'/records/r');
  assert.equal(calls[2][0],'/records');assert.deepEqual(calls[2][1].json,payload);assert.equal(calls[2][1].headers['Idempotency-Key'],'fixture-request');
  await assert.rejects(createTextRecord({records,uploads:{publishText:async()=>{throw new Error('publish failed');}},payload,idempotencyKey:'fixture-request'}),e=>e.savedRecordId==='r');
});
test('runtime no longer exposes unsupported destructive actions or health route',()=>{
  const runtime=fs.readFileSync('src/shared/api/runtime.js','utf8');
  assert.doesNotMatch(runtime,/system\/health\/db|deleteCurrentAccount|deleteFamily/);
  assert.equal(require('./familyRooms.cjs').createFamilyRoomsApi({}).remove,undefined);
  const nav=fs.readFileSync('src/navigation/AppNavigator.js','utf8');
  assert.match(nav,/onDeleteAccount=\{null\}/);assert.match(nav,/onDeleteFamily=\{null\}/);
});

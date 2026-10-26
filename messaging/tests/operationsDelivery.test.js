const {test}=require('node:test');const assert=require('node:assert/strict');
const {createOperationsBridge,safeFailure}=require('../src/routes/operationsBridge');
const key='x'.repeat(40),requestId='aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa',phone='+19197537167';
function fixture({failure,dbFailure=false}={}){
 const records=new Map(),logs=[];let sends=0;
 const prisma={message:{findUnique:async({where})=>{if(dbFailure)throw Object.assign(Error('PRIVATE DATABASE CONNECTION'),{code:'P2021'});return records.get(where.id)},create:async({data})=>records.set(data.id,{...data,conversation:{customerPhone:phone}}),update:async({where,data})=>Object.assign(records.get(where.id),data)},conversation:{findFirst:async()=>({id:'conversation'})}};
 const quo={sendMessage:async()=>{sends++;if(failure)throw failure;return {id:'provider-id',status:'queued'}}};
 const handler=createOperationsBridge({prisma,quo,token:()=>key,logFailure:d=>logs.push(d)});
 async function call(path='/delivery-status',body={requestId},method='GET',authorization='Bearer '+key){const res={code:200,set(){},status(n){this.code=n;return this},json(d){this.value=d;return this}};await handler({path,query:body,body,method,headers:{authorization}},res);return res;}
 return {records,logs,call,get sends(){return sends},send:()=>call('/send',{requestId,phone,body:'Owner-authorized delivery test',confirmed:true},'POST')};
}
test('delivery check authenticates and missing reservation never sends',async()=>{const f=fixture();assert.equal((await f.call('/delivery-status',{requestId},'GET','bad')).code,401);const r=await f.call();assert.equal(r.value.state,'not_attempted');assert.equal(r.value.mayRetry,false);assert.equal(f.sends,0);});
test('delivery check exposes only reservation outcome and receipt',async()=>{const f=fixture();await f.send();const r=await f.call();assert.equal(r.value.state,'accepted');assert.equal(r.value.providerId,'provider-id');assert.doesNotMatch(JSON.stringify(r.value),/9197537167|Owner-authorized/);assert.equal(f.sends,1);});
test('definite provider rejection retains safe status/code and never replays',async()=>{const f=fixture({failure:Object.assign(Error('PRIVATE TOKEN AND PHONE'),{status:400,code:'0206400',body:{token:'SECRET'}})});const r=await f.send();assert.equal(r.value.state,'rejected');assert.equal(r.value.providerCode,'0206400');assert.equal((await f.call()).value.state,'rejected');assert.equal((await f.send()).code,409);assert.equal(f.sends,1);assert.doesNotMatch(JSON.stringify([r.value,f.logs]),/PRIVATE|TOKEN|SECRET|9197537167/);});
test('timeout remains uncertain and is never made retryable by a read',async()=>{const f=fixture({failure:Error('timeout')});assert.equal((await f.send()).value.state,'uncertain');const r=await f.call();assert.equal(r.value.state,'uncertain');assert.equal(r.value.mayRetry,false);assert.equal((await f.send()).code,409);assert.equal(f.sends,1);});
test('database failure before reservation distinguishes not-sent from provider uncertainty',async()=>{const f=fixture({dbFailure:true});const r=await f.send();assert.equal(r.value.state,'not_sent');assert.equal(r.value.providerAttempted,false);assert.equal(f.sends,0);assert.equal(f.logs[0].code,'P2021');assert.doesNotMatch(JSON.stringify([r.value,f.logs]),/PRIVATE DATABASE/);});
test('failure diagnostics never serialize raw messages, keys or responses',()=>{assert.deepEqual(safeFailure({status:403,code:'0200403',message:'SECRET',body:{Authorization:'SECRET'}}),{status:403,code:'0200403'});assert.deepEqual(safeFailure({status:'not-a-status',code:'SECRET-token'}),{status:null,code:null});});

test('payment rejection stays rejected on checks and duplicate clicks with actionable redacted guidance',async()=>{
 const f=fixture({failure:Object.assign(Error('PRIVATE account balance 0 and TOKEN'),{status:402,code:'0200402',body:{balance:0}})});
 const first=await f.send();assert.equal(first.code,502);
 for(const outcome of [first,await f.call(),await f.send()]){
  assert.equal(outcome.value.state,'rejected');assert.equal(outcome.value.mayRetry,false);
  assert.equal(outcome.value.providerStatus,402);assert.equal(outcome.value.providerCode,'0200402');
  assert.match(outcome.value.nextAction,/Check Quo billing/);
  assert.match(outcome.value.nextAction,/does not establish the current balance/);
  assert.doesNotMatch(JSON.stringify(outcome.value),/PRIVATE|TOKEN|balance 0|9197537167/);
 }
 assert.equal(f.sends,1);
});
test('rejection guidance distinguishes permissions and limits without retrying',async()=>{
 for(const [status,expected] of [[401,/connection and sending permissions/],[403,/connection and sending permissions/],[429,/sending limits/],[422,/recipient/]]){
  const f=fixture({failure:Object.assign(Error('PRIVATE'),{status})});
  assert.match((await f.send()).value.nextAction,expected);
  const repeat=await f.send();assert.equal(repeat.code,409);assert.equal(repeat.value.state,'rejected');
  assert.equal(repeat.value.mayRetry,false);assert.equal(f.sends,1);
 }
});

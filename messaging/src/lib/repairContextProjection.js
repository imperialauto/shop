// Read-only projection. Never forward legacy signing tokens or arbitrary fields.
function projectRepairContext(value){
 if(!value||typeof value.found!=='boolean')throw Error('Invalid legacy context');
 const base={found:value.found,matchReviewRequired:true,coverage:'Legacy portal phone lookup: saved vehicles and latest open repair order/invoice only. Review the customer and vehicle match. This is not a complete historical export or provider payment verification.'};
 if(!value.found)return base;
 const text=(v,max=500)=>{if(v==null)return '';if(typeof v!=='string'||v.length>max)throw Error('Invalid legacy text');return v;};
 const id=v=>{if(!Number.isSafeInteger(v)||v<1)throw Error('Invalid legacy identifier');return v;};
 const date=v=>v==null?null:text(v,50);
 if(!value.customer||!Array.isArray(value.vehicles)||value.vehicles.length>50)throw Error('Invalid legacy customer');
 const customer={id:id(value.customer.id),name:text(value.customer.name,200),customerType:text(value.customer.customerType,30)};
 const vehicles=value.vehicles.map(v=>({id:id(v.id),year:v.year==null?null:id(v.year),make:text(v.make,100),model:text(v.model,200),engine:text(v.engine,200)}));
 const ro=value.openRepairOrder,invoice=value.openInvoice;
 const openRepairOrder=ro?{id:id(ro.id),status:text(ro.status,50),concern:text(ro.concern,20000),promisedDate:date(ro.promisedDate),signedAt:date(ro.signedAt)}:null;
 let openInvoice=null;if(invoice){if(typeof invoice.total!=='number'||!Number.isFinite(invoice.total)||invoice.total<0)throw Error('Invalid legacy amount');openInvoice={id:id(invoice.id),status:text(invoice.status,50),total:invoice.total,signedAt:date(invoice.signedAt)};}
 return {...base,customer,vehicles,openRepairOrder,openInvoice};
}
module.exports={projectRepairContext};

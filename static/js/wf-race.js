(function(){
  'use strict';
  var urls=JSON.parse(document.getElementById('wf-race-urls').textContent);
  var brief=document.getElementById('wf-brief'), race=document.getElementById('wf-race');
  var startBtn=document.getElementById('wf-start-race'), shell=document.getElementById('wf-track-shell');
  var car=document.getElementById('wf-car'), road=document.getElementById('wf-road');
  var timerEl=document.getElementById('wf-race-timer'), progressEl=document.getElementById('wf-race-progress');
  var obstacleEl=document.getElementById('wf-obstacles'), collisionEl=document.getElementById('wf-collisions'), message=document.getElementById('wf-race-message');
  var obstacles=Array.prototype.slice.call(document.querySelectorAll('.wf-obstacle'));
  var x=50, collisions=0, cleared=0, started=false, finished=false, endsAt=0, lastSync=0, keys={};
  var duration=720, courseSeconds=180, hitCooldown={};
  var laneCenters=[20,50,80];
  var obstacleProgress=[.02,.14,.26,.38,.50,.62];

  obstacles.forEach(function(o,i){
    var lane=[1,0,2,1,2,0][i];
    o.dataset.lane=lane;
    o.style.left=laneCenters[lane]+'%';
    o.style.width='25%';
    o.style.top='0';
  });
  document.getElementById('wf-finish').style.top='0';

  function post(url,data){var body=new URLSearchParams();Object.keys(data||{}).forEach(function(k){body.append(k,data[k]);});var csrf=(document.cookie.match(/(?:^|; )csrftoken=([^;]+)/)||[])[1]||'';return fetch(url,{method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf),'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded'},body:body,credentials:'same-origin'}).then(function(r){return r.json();});}
  function fmt(s){s=Math.max(0,Math.ceil(s));return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
  function showRace(){brief.hidden=true;race.hidden=false;shell.focus();}
  function start(){if(started)return;startBtn.disabled=true;startBtn.textContent='STARTING…';post(urls.start,{}).then(function(d){if(d.error){startBtn.disabled=false;startBtn.textContent='START RACE';alert(d.error);return;}duration=d.duration||720;endsAt=Date.now()+d.remaining*1000;started=true;showRace();message.textContent='GO!';setTimeout(function(){message.textContent='';},800);requestAnimationFrame(loop);}).catch(function(){startBtn.disabled=false;startBtn.textContent='START RACE';alert('Could not start the race. Please try again.');});}
  function move(){var left=keys.ArrowLeft||keys.a||keys.A, right=keys.ArrowRight||keys.d||keys.D;if(left)x-=.75;if(right)x+=.75;x=Math.max(8,Math.min(92,x));car.style.left=x+'%';}
  function rect(el){return el.getBoundingClientRect();}
  function intersects(a,b){return !(a.right<b.left||a.left>b.right||a.bottom<b.top||a.top>b.bottom);}
  function positionCourse(elapsed){
    var progress=Math.min(1,elapsed/courseSeconds);
    obstacles.forEach(function(o,i){
      var local=progress-obstacleProgress[i];
      var y=-110+local*(road.clientHeight+1200);
      o.style.top=y+'px';
      o.style.opacity=(local<-.12||local>1.03)?'0': '1';
    });
    var finish=document.getElementById('wf-finish');
    finish.style.top=(-80+(progress*Math.max(200, road.clientHeight-135)))+'px';
    finish.style.opacity=progress>.92?'1':'0';
  }
  function checkObstacles(){
    var cr=rect(car);
    obstacles.forEach(function(o,i){
      var or=rect(o);
      if(or.bottom<cr.top){
        if(o.dataset.cleared!=='1' && parseFloat(o.style.top)>0){o.dataset.cleared='1';cleared++;obstacleEl.textContent=cleared+'/6';progressEl.style.width=(cleared/6*100)+'%';}
        return;
      }
      if(o.style.opacity==='0')return;
      if(intersects(cr,or)){
        var now=Date.now();
        if(!hitCooldown[i]||now-hitCooldown[i]>900){
          hitCooldown[i]=now;collisions++;collisionEl.textContent=collisions;
          car.animate([{transform:'translateX(-50%) scale(1)'},{transform:'translateX(-50%) scale(.84) rotate(-3deg)'},{transform:'translateX(-50%) scale(1)'}],{duration:240});
        }
      }
    });
  }
  function finishCheck(elapsed){
    var finish=document.getElementById('wf-finish');
    if(cleared===6 && elapsed>=courseSeconds && !finished){
      finished=true;message.textContent='CSS FIXED!';complete();
    }
  }
  function complete(){post(urls.complete,{obstacles:cleared,collisions:collisions}).then(function(d){if(d.redirect)window.location.href=d.redirect;else if(d.error){message.textContent=d.error;finished=false;}}).catch(function(){finished=false;message.textContent='NETWORK ERROR — RETRYING';setTimeout(complete,1200);});}
  function sync(){if(!started||finished)return;fetch(urls.state,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){if(d.expired&&!finished){finished=true;message.textContent="TIME'S UP";setTimeout(function(){window.location.reload();},900);}}).catch(function(){});}
  function loop(){
    if(!started||finished)return;
    move();
    var elapsed=Math.max(0,courseSeconds-(Math.max(0,(endsAt-Date.now())/1000)));
    // Course advances independently of the 12-minute maximum, so skilled players finish early.
    positionCourse(elapsed);checkObstacles();finishCheck(elapsed);
    var remain=Math.max(0,(endsAt-Date.now())/1000);
    timerEl.textContent=fmt(remain);
    timerEl.classList.toggle('warn',remain<=300&&remain>120);timerEl.classList.toggle('danger',remain<=120);
    if(remain<=0){finished=true;message.textContent="TIME'S UP";setTimeout(function(){window.location.reload();},1200);return;}
    if(Date.now()-lastSync>5000){lastSync=Date.now();sync();}
    requestAnimationFrame(loop);
  }
  startBtn.addEventListener('click',start);
  window.addEventListener('keydown',function(e){if(['ArrowLeft','ArrowRight','a','d','A','D'].indexOf(e.key)>=0){keys[e.key]=true;e.preventDefault();}});
  window.addEventListener('keyup',function(e){keys[e.key]=false;});
  shell.addEventListener('click',function(e){var r=shell.getBoundingClientRect();x=((e.clientX-r.left)/r.width)*100;});
}());

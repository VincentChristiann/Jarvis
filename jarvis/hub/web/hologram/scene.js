// The "hologram": floating translucent panels in 3D, with head-tracked parallax and ray-picked hover.
import * as THREE from "/vendor/three/three.module.min.js";

const CYAN = "#4cc9f0";

function panelTexture(w, h, draw) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  const g = c.getContext("2d"); draw(g, w, h);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 4;
  return tex;
}
function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}
function drawCard(g, w, h, {title, sub, glyph, state = "idle", online = true}) {
  g.clearRect(0, 0, w, h);
  const hot = state !== "idle";
  roundRect(g, 6, 6, w - 12, h - 12, 22);
  g.fillStyle = hot ? "rgba(76,201,240,.30)" : "rgba(20,40,80,.45)"; g.fill();
  g.lineWidth = hot ? 5 : 2.5; g.strokeStyle = state === "press" ? "#3ddc97" : CYAN; g.shadowColor = CYAN; g.shadowBlur = hot ? 24 : 10; g.stroke();
  g.shadowBlur = 0; g.fillStyle = "#e6ecff"; g.textAlign = "center"; g.textBaseline = "middle";
  if (glyph) { g.font = `${h * 0.38}px system-ui`; g.fillText(glyph, w / 2, h * (sub ? 0.38 : 0.45)); }
  g.font = `600 ${h * (glyph ? 0.15 : 0.2)}px system-ui`;
  g.fillText(title, w / 2, h * (glyph ? 0.72 : 0.42));
  if (sub) { g.font = `${h * 0.12}px system-ui`; g.fillStyle = online ? "#3ddc97" : "#8a97bd"; g.fillText(sub, w / 2, h * (glyph ? 0.88 : 0.68)); }
}

export class HoloScene {
  constructor(canvas) {
    this.renderer = new THREE.WebGLRenderer({canvas, alpha: true, antialias: true});
    this.renderer.setClearColor(0x000000, 0);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 50);
    this.camera.position.set(0, 0, 6);
    this.head = {x: 0.5, y: 0.5}; this.headTarget = {x: 0.5, y: 0.5};
    this.ray = new THREE.Raycaster();
    this.items = [];           // pickable meshes
    this.hover = null; this.pressed = null;
    this.group = new THREE.Group(); this.scene.add(this.group);
    this._backdrop();
    this.resize();
    addEventListener("resize", () => this.resize());
  }

  _backdrop() {
    const grid = new THREE.GridHelper(30, 40, 0x1c6f8c, 0x0f3a50);
    grid.position.y = -2.4; grid.material.transparent = true; grid.material.opacity = 0.45; this.scene.add(grid);
    const ring = new THREE.Mesh(new THREE.RingGeometry(3.7, 3.74, 128), new THREE.MeshBasicMaterial({color: 0x4cc9f0, transparent: true, opacity: 0.35, side: THREE.DoubleSide}));
    ring.position.z = -2.2; this.scene.add(ring); this.ring = ring;
  }

  resize() {
    const w = innerWidth, h = innerHeight;
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }

  _add(w, h, x, y, z, drawArgs, action) {
    const geo = new THREE.PlaneGeometry(w, h);
    const mat = new THREE.MeshBasicMaterial({map: panelTexture(512, Math.round(512 * h / w), (g, W, H) => drawCard(g, W, H, drawArgs)),
                                             transparent: true, depthWrite: false, blending: THREE.AdditiveBlending});
    const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z);
    m.userData = {action, drawArgs, w, h, baseY: y, phase: Math.random() * 6};
    this.group.add(m); this.items.push(m);
    return m;
  }

  _redraw(m, state) {
    const {drawArgs} = m.userData;
    const c = m.material.map.image;
    drawCard(c.getContext("2d"), c.width, c.height, {...drawArgs, state});
    m.material.map.needsUpdate = true;
  }

  // devices: [{id,name,online}], selected: id|null, actions: [{label,glyph,action}]
  build(devices, selected, actions) {
    for (const m of this.items) { this.group.remove(m); m.material.map.dispose(); m.material.dispose(); m.geometry.dispose(); }
    this.items = []; this.hover = null; this.pressed = null;
    const n = devices.length || 1;
    devices.forEach((d, i) => {
      const x = (i - (n - 1) / 2) * 2.5, arc = -Math.abs(i - (n - 1) / 2) * 0.25;
      this._add(2.2, 1.2, x, selected ? 1.9 : 0.6, arc + (d.id === selected ? 0.5 : 0),
        {title: d.name, sub: d.online ? "online" : "offline", online: d.online, state: d.id === selected ? "hover" : "idle"},
        {kind: "device", id: d.id});
    });
    if (selected) {
      actions.forEach((a, i) => {
        const cols = 4, col = i % cols, row = Math.floor(i / cols);
        this._add(1.15, 1.0, (col - (cols - 1) / 2) * 1.35, 0.35 - row * 1.15, 0.9, {title: a.label, glyph: a.glyph}, {kind: "button", ...a.action, label: a.label});
      });
    }
  }

  setHead(h) { if (h) this.headTarget = h; }

  // pointer in [0,1] (already mirrored) or null; returns the hovered action or null
  setPointer(p) {
    let hit = null;
    if (p) {
      this.ray.setFromCamera(new THREE.Vector2(p.x * 2 - 1, -(p.y * 2 - 1)), this.camera);
      const h = this.ray.intersectObjects(this.items, false)[0];
      hit = h ? h.object : null;
    }
    if (hit !== this.hover) {
      if (this.hover) this._redraw(this.hover, "idle");
      if (hit) this._redraw(hit, "hover");
      this.hover = hit;
    }
    return hit ? hit.userData.action : null;
  }

  press(on) {
    const m = on ? this.hover : this.pressed;
    if (m) this._redraw(m, on ? "press" : (m === this.hover ? "hover" : "idle"));
    this.pressed = on ? m : null;
  }

  render(t) {
    this.head.x += (this.headTarget.x - this.head.x) * 0.15; this.head.y += (this.headTarget.y - this.head.y) * 0.15;
    // Off-axis feel: the camera slides with your head and keeps looking at the middle of the scene.
    this.camera.position.set((this.head.x - 0.5) * 3.2, -(this.head.y - 0.5) * 2.0 + 0.2, 6);
    this.camera.lookAt(0, 0.3, 0);
    for (const m of this.items) m.position.y = m.userData.baseY + Math.sin(t / 900 + m.userData.phase) * 0.04;
    this.ring.rotation.z = t / 8000;
    this.renderer.render(this.scene, this.camera);
  }
}

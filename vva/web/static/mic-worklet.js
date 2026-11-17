// 麦克风采集：把每一块音频（Float32，单声道）转发给页面，由页面重采样后发给服务端
class Cap extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("cap", Cap);

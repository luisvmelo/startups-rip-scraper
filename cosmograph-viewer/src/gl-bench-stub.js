/** Substituição mínima de gl-bench para o bundler (o pacote original não expõe default ESM). */
export default class GlBenchStub {
  constructor() {}
  begin() {}
  end() {}
  nextFrame() {}
}

import { useRouter } from 'next/router';
import { useParams } from 'next/navigation';
import { ComponentRef, useEffect, useMemo, useRef, useState } from 'react';
import { message } from 'antd';
import { Path } from '@/utils/enum';
import useHomeSidebar from '@/hooks/useHomeSidebar';
import SiderLayout from '@/components/layouts/SiderLayout';
import Prompt from '@/components/pages/home/prompt';
import useAskPrompt, {
  getIsFinished,
  isRecommendedFinished,
} from '@/hooks/useAskPrompt';
import useModalAction from '@/hooks/useModalAction';
import PromptThread from '@/components/pages/home/promptThread';
import SaveAsViewModal from '@/components/modals/SaveAsViewModal';
import {
  useCreateThreadResponseMutation,
  useThreadQuery,
  useThreadResponseLazyQuery,
  useGenerateThreadRecommendationQuestionsMutation,
  useGetThreadRecommendationQuestionsLazyQuery,
} from '@/apollo/client/graphql/home.generated';
import { useCreateViewMutation } from '@/apollo/client/graphql/view.generated';

export default function HomeThread() {
  const $prompt = useRef<ComponentRef<typeof Prompt>>(null);
  const router = useRouter();
  const params = useParams();
  const homeSidebar = useHomeSidebar();
  const threadId = useMemo(() => Number(params?.id) || null, [params]);
  const askPrompt = useAskPrompt(threadId);
  const saveAsViewModal = useModalAction();

  const [showRecommendedQuestions, setShowRecommendedQuestions] =
    useState<boolean>(false);

  const [createViewMutation, { loading: creating }] = useCreateViewMutation({
    onError: (error) => console.error(error),
    onCompleted: () => message.success('Successfully created view.'),
  });

  const { data, updateQuery: updateThreadQuery } = useThreadQuery({
    variables: { threadId },
    fetchPolicy: 'cache-and-network',
    skip: threadId === null,
    onError: () => router.push(Path.Home),
  });
  const [createThreadResponse] = useCreateThreadResponseMutation({
    onCompleted(next) {
      const nextResponse = next.createThreadResponse;
      updateThreadQuery((prev) => {
        return {
          ...prev,
          thread: {
            ...prev.thread,
            responses: [...prev.thread.responses, nextResponse],
          },
        };
      });
    },
  });
  const [fetchThreadResponse, threadResponseResult] =
    useThreadResponseLazyQuery({
      pollInterval: 1000,
      onCompleted(next) {
        const nextResponse = next.threadResponse;
        updateThreadQuery((prev) => ({
          ...prev,
          thread: {
            ...prev.thread,
            responses: prev.thread.responses.map((response) =>
              response.id === nextResponse.id ? nextResponse : response,
            ),
          },
        }));
      },
    });

  const [generateThreadRecommendationQuestions] =
    useGenerateThreadRecommendationQuestionsMutation();

  const [
    fetchThreadRecommendationQuestions,
    threadRecommendationQuestionsResult,
  ] = useGetThreadRecommendationQuestionsLazyQuery({
    pollInterval: 1000,
  });

  const thread = useMemo(() => data?.thread || null, [data]);
  const threadResponse = useMemo(
    () => threadResponseResult.data?.threadResponse || null,
    [threadResponseResult.data],
  );
  const isFinished = useMemo(
    () => getIsFinished(threadResponse?.status),
    [threadResponse],
  );

  // stop all requests when change thread
  useEffect(() => {
    askPrompt.onStopPolling();
    threadResponseResult.stopPolling();
    threadRecommendationQuestionsResult.stopPolling();
    $prompt.current?.close();

    if (threadId !== null) {
      fetchThreadRecommendationQuestions({ variables: { threadId } });
      setShowRecommendedQuestions(true);
    }
  }, [threadId]);

  useEffect(() => {
    const unfinishedRespose = (thread?.responses || []).find(
      (response) => !getIsFinished(response.status),
    );

    if (unfinishedRespose) {
      fetchThreadResponse({ variables: { responseId: unfinishedRespose.id } });
    }

    // store thread questions for instant recommended questions
    const questions = thread?.responses.flatMap((res) => res.question || []);
    if (questions) askPrompt.onStoreThreadQuestions(questions);
  }, [thread]);

  useEffect(() => {
    if (isFinished) {
      threadResponseResult.stopPolling();
      setShowRecommendedQuestions(true);
    }
  }, [isFinished]);

  const recommendedQuestions = useMemo(
    () =>
      threadRecommendationQuestionsResult.data
        ?.getThreadRecommendationQuestions || null,
    [threadRecommendationQuestionsResult.data],
  );

  useEffect(() => {
    if (isRecommendedFinished(recommendedQuestions?.status)) {
      threadRecommendationQuestionsResult.stopPolling();
    }
  }, [recommendedQuestions]);

  const result = useMemo(
    () => ({
      thread,
      recommendedQuestions,
      showRecommendedQuestions,
    }),
    [thread, recommendedQuestions, showRecommendedQuestions],
  );

  const onSelect = async (payload) => {
    try {
      askPrompt.onStopPolling();
      const response = await createThreadResponse({
        variables: { threadId: thread.id, data: payload },
      });
      await generateThreadRecommendationQuestions({
        variables: { threadId: thread.id },
      });
      setShowRecommendedQuestions(false);
      await fetchThreadResponse({
        variables: { responseId: response.data.createThreadResponse.id },
      });
      fetchThreadRecommendationQuestions({
        variables: { threadId: thread.id },
      });
    } catch (error) {
      console.error(error);
    }
  };

  return (
    <SiderLayout loading={false} sidebar={homeSidebar}>
      <PromptThread
        data={result}
        onOpenSaveAsViewModal={saveAsViewModal.openModal}
        onSelect={onSelect}
      />
      <div className="py-12" />
      <Prompt ref={$prompt} {...askPrompt} onSelect={onSelect} />
      <SaveAsViewModal
        {...saveAsViewModal.state}
        loading={creating}
        onClose={saveAsViewModal.closeModal}
        onSubmit={async (values) => {
          await createViewMutation({
            variables: { data: values },
          });
        }}
      />
    </SiderLayout>
  );
}

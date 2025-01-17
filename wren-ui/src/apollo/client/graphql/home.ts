import { gql } from '@apollo/client';

const COMMON_ERROR = gql`
  fragment CommonError on Error {
    code
    shortMessage
    message
    stacktrace
  }
`;

const COMMON_RESPONSE = gql`
  fragment CommonResponse on ThreadResponse {
    id
    question
    status
    detail {
      sql
      description
      steps {
        summary
        sql
        cteName
      }
      view {
        id
        name
        statement
        displayName
      }
    }
  }
`;

const COMMON_RECOMMENDED_QUESTIONS_TASK = gql`
  fragment CommonRecommendedQuestionsTask on RecommendedQuestionsTask {
    status
    questions {
      question
      category
      sql
    }
    error {
      ...CommonError
    }
  }

  ${COMMON_ERROR}
`;

export const SUGGESTED_QUESTIONS = gql`
  query SuggestedQuestions {
    suggestedQuestions {
      questions {
        label
        question
      }
    }
  }
`;

export const ASKING_TASK = gql`
  query AskingTask($taskId: String!) {
    askingTask(taskId: $taskId) {
      status
      type
      candidates {
        sql
        type
        view {
          id
          name
          statement
          displayName
        }
      }
      error {
        ...CommonError
      }
    }
  }
  ${COMMON_ERROR}
`;

export const THREADS = gql`
  query Threads {
    threads {
      id
      summary
    }
  }
`;

export const THREAD = gql`
  query Thread($threadId: Int!) {
    thread(threadId: $threadId) {
      id
      sql
      responses {
        ...CommonResponse
        error {
          ...CommonError
        }
      }
    }
  }
  ${COMMON_RESPONSE}
  ${COMMON_ERROR}
`;

export const THREAD_RESPONSE = gql`
  query ThreadResponse($responseId: Int!) {
    threadResponse(responseId: $responseId) {
      ...CommonResponse
      error {
        ...CommonError
      }
    }
  }
  ${COMMON_RESPONSE}
  ${COMMON_ERROR}
`;

export const CREATE_ASKING_TASK = gql`
  mutation CreateAskingTask($data: AskingTaskInput!) {
    createAskingTask(data: $data) {
      id
    }
  }
`;

export const CANCEL_ASKING_TASK = gql`
  mutation CancelAskingTask($taskId: String!) {
    cancelAskingTask(taskId: $taskId)
  }
`;

export const CREATE_THREAD = gql`
  mutation CreateThread($data: CreateThreadInput!) {
    createThread(data: $data) {
      id
      sql
    }
  }
`;

export const CREATE_THREAD_RESPONSE = gql`
  mutation CreateThreadResponse(
    $threadId: Int!
    $data: CreateThreadResponseInput!
  ) {
    createThreadResponse(threadId: $threadId, data: $data) {
      ...CommonResponse
      error {
        code
        shortMessage
        message
        stacktrace
      }
    }
  }
  ${COMMON_RESPONSE}
  ${COMMON_ERROR}
`;

export const UPDATE_THREAD = gql`
  mutation UpdateThread(
    $where: ThreadUniqueWhereInput!
    $data: UpdateThreadInput!
  ) {
    updateThread(where: $where, data: $data) {
      id
      sql
      summary
    }
  }
`;

export const DELETE_THREAD = gql`
  mutation DeleteThread($where: ThreadUniqueWhereInput!) {
    deleteThread(where: $where)
  }
`;

export const PREVIEW_DATA = gql`
  mutation PreviewData($where: PreviewDataInput!) {
    previewData(where: $where)
  }
`;

export const GET_NATIVE_SQL = gql`
  query GetNativeSQL($responseId: Int!) {
    nativeSql(responseId: $responseId)
  }
`;

export const CREATE_INSTANT_RECOMMENDED_QUESTIONS = gql`
  mutation CreateInstantRecommendedQuestions(
    $data: InstantRecommendedQuestionsInput!
  ) {
    createInstantRecommendedQuestions(data: $data) {
      id
    }
  }
`;

export const INSTANT_RECOMMENDED_QUESTIONS = gql`
  query InstantRecommendedQuestions($taskId: String!) {
    instantRecommendedQuestions(taskId: $taskId) {
      ...CommonRecommendedQuestionsTask
    }
  }
  ${COMMON_RECOMMENDED_QUESTIONS_TASK}
`;

export const GET_THREAD_RECOMMENDATION_QUESTIONS = gql`
  query GetThreadRecommendationQuestions($threadId: Int!) {
    getThreadRecommendationQuestions(threadId: $threadId) {
      ...CommonRecommendedQuestionsTask
    }
  }

  ${COMMON_RECOMMENDED_QUESTIONS_TASK}
`;

export const GET_PROJECT_RECOMMENDATION_QUESTIONS = gql`
  query GetProjectRecommendationQuestions {
    getProjectRecommendationQuestions {
      ...CommonRecommendedQuestionsTask
    }
  }

  ${COMMON_RECOMMENDED_QUESTIONS_TASK}
`;

export const GENERATE_PROJECT_RECOMMENDATION_QUESTIONS = gql`
  mutation GenerateProjectRecommendationQuestions {
    generateProjectRecommendationQuestions
  }
`;

export const GENERATE_THREAD_RECOMMENDATION_QUESTIONS = gql`
  mutation GenerateThreadRecommendationQuestions($threadId: Int!) {
    generateThreadRecommendationQuestions(threadId: $threadId)
  }
`;
